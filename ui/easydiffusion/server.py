"""server.py: FastAPI SD-UI Web Host.
Notes:
    async endpoints always run on the main thread. Without they run on the thread pool.
"""
import datetime
import mimetypes
import os
import traceback
import random
from typing import List, Union

from easydiffusion import app, model_manager, task_manager, package_manager
from easydiffusion.tasks import RenderTask, FilterTask
from easydiffusion.types import (
    GenerateImageRequest,
    FilterImageRequest,
    MergeRequest,
    TaskData,
    RenderTaskData,
    ModelsData,
    OutputFormatData,
    SaveToDiskData,
    convert_legacy_render_req_to_new,
)
from easydiffusion.utils import log
from fastapi import Depends, FastAPI, Security, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import timedelta
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Extra
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from pycloudflared import try_cloudflare
from googletrans import Translator

SECRET_KEY = "83daa0256a2289b0fb23693bf1f6034d44396675749244721a2b20e896e11662"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 365

db = {
    "timoscow": {
        "username": "timoscow",
        "full_name": "Tim Kar",
        "email": "tim-kap@ya.ru",
        "hashed_password": "$2b$12$B54TYS7FNWAsnQBB21Hn9uWOAyFgCcKc2jvvBU9N1GNZwlmhmrfMe",
        "disabled": False
    }
}

class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    username: str or None = None


class User(BaseModel):
    username: str
    email: str or None = None
    full_name: str or None = None
    disabled: bool or None = None


class UserInDB(User):
    hashed_password: str


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


log.info(f"started in {app.SD_DIR}")
log.info(f"started at {datetime.datetime.now():%x %X}")

server_api = FastAPI()


def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


def get_user(db, username: str):
    if username in db:
        user_data = db[username]
        return UserInDB(**user_data)


def authenticate_user(db, username: str, password: str):
    user = get_user(db, username)
    if not user:
        return False
    if not verify_password(password, user.hashed_password):
        return False

    return user


def create_access_token(data: dict, expires_delta: timedelta or None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.datetime.utcnow() + expires_delta
    else:
        expire = datetime.datetime.utcnow() + timedelta(minutes=150)

    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credential_exception = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credential_exception

        token_data = TokenData(username=username)
    except JWTError:
        raise credential_exception

    user = get_user(db, username=token_data.username)
    if user is None:
        raise credential_exception

    return user


async def get_current_active_user(current_user: UserInDB = Depends(get_current_user)):
    if current_user.disabled:
        raise HTTPException(status_code=400, detail="Inactive user")

    return current_user

NOCACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}
PROTECTED_CONFIG_KEYS = ("block_nsfw",)  # can't change these via the HTTP API


class NoCacheStaticFiles(StaticFiles):
    def __init__(self, directory: str):
        # follow_symlink is only available on fastapi >= 0.92.0
        if os.path.islink(directory):
            super().__init__(directory=os.path.realpath(directory))
        else:
            super().__init__(directory=directory)

    def is_not_modified(self, response_headers, request_headers) -> bool:
        if "content-type" in response_headers and (
            "javascript" in response_headers["content-type"] or "css" in response_headers["content-type"]
        ):
            response_headers.update(NOCACHE_HEADERS)
            return False

        return super().is_not_modified(response_headers, request_headers)


class SetAppConfigRequest(BaseModel, extra=Extra.allow):
    update_branch: str = None
    render_devices: Union[List[str], List[int], str, int] = None
    model_vae: str = None
    ui_open_browser_on_start: bool = None
    listen_to_network: bool = None
    listen_port: int = None
    use_v3_engine: bool = True
    models_dir: str = None


def init():
    mimetypes.init()
    mimetypes.add_type("text/css", ".css")

    if os.path.isdir(app.CUSTOM_MODIFIERS_DIR):
        server_api.mount(
            "/media/modifier-thumbnails/custom",
            NoCacheStaticFiles(directory=app.CUSTOM_MODIFIERS_DIR),
            name="custom-thumbnails",
        )

    server_api.mount(
        "/media",
        NoCacheStaticFiles(directory=os.path.join(app.SD_UI_DIR, "media")),
        name="media",
    )

    for plugins_dir, dir_prefix in app.UI_PLUGINS_SOURCES:
        server_api.mount(
            f"/plugins/{dir_prefix}",
            NoCacheStaticFiles(directory=plugins_dir),
            name=f"plugins-{dir_prefix}",
        )

    @server_api.post("/token", response_model=Token)
    async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
        user = authenticate_user(db, form_data.username, form_data.password)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Incorrect username or password", headers={"WWW-Authenticate": "Bearer"})
        access_token_expires = timedelta(days=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": user.username}, expires_delta=access_token_expires)
        return {"access_token": access_token, "token_type": "bearer"}


    @server_api.get("/users/me/", response_model=User)
    async def read_users_me(current_user: User = Depends(get_current_active_user)):
        return current_user


    @server_api.get("/users/me/items")
    async def read_own_items(current_user: User = Depends(get_current_active_user)):
        return [{"item_id": 1, "owner": current_user}]




    @server_api.post("/app_config")
    async def set_app_config(req: SetAppConfigRequest, current_user: User = Depends(get_current_active_user)):
        return set_app_config_internal(req)

    @server_api.get("/get/{key:path}")
    def read_web_data(key: str = None, scan_for_malicious: bool = True, current_user: User = Depends(get_current_active_user)):
        return read_web_data_internal(key, scan_for_malicious=scan_for_malicious)

    @server_api.get("/ping")  # Get server and optionally session status.
    def ping(session_id: str = None, current_user: User = Depends(get_current_active_user)):
        return ping_internal(session_id)

    @server_api.post("/render")
    async def render(req: dict, current_user: User = Depends(get_current_active_user)):
            return render_internal(req)

    @server_api.post("/filter")
    def render(req: dict, current_user: User = Depends(get_current_active_user)):
        return filter_internal(req)

    @server_api.post("/model/merge")
    def model_merge(req: dict, current_user: User = Depends(get_current_active_user)):
        print(req)
        return model_merge_internal(req)

    @server_api.get("/image/stream/{task_id:int}")
    def stream(task_id: int, current_user: User = Depends(get_current_active_user)):
        return stream_internal(task_id)

    @server_api.get("/image/stop")
    def stop(task: int, current_user: User = Depends(get_current_active_user)):
        return stop_internal(task)

    @server_api.get("/image/tmp/{task_id:int}/{img_id:int}")
    def get_image(task_id: int, img_id: int, current_user: User = Depends(get_current_active_user)):
        return get_image_internal(task_id, img_id)

    @server_api.post("/tunnel/cloudflare/start")
    def start_cloudflare_tunnel(req: dict, current_user: User = Depends(get_current_active_user)):
        return start_cloudflare_tunnel_internal(req)

    @server_api.post("/tunnel/cloudflare/stop")
    def stop_cloudflare_tunnel(req: dict, current_user: User = Depends(get_current_active_user)):
        return stop_cloudflare_tunnel_internal(req)

    @server_api.post("/package/{package_name:str}")
    def modify_package(package_name: str, req: dict, current_user: User = Depends(get_current_active_user)):
        return modify_package_internal(package_name, req)

    @server_api.get("/sha256/{obj_path:path}")
    def get_sha256(obj_path: str, current_user: User = Depends(get_current_active_user)):
        return get_sha256_internal(obj_path)

    @server_api.get("/")
    def read_root(current_user: User = Depends(get_current_active_user)):
        return FileResponse(os.path.join(app.SD_UI_DIR, "index.html"), headers=NOCACHE_HEADERS)

    @server_api.on_event("shutdown")
    def shutdown_event(current_user: User = Depends(get_current_active_user)):  # Signal render thread to close on shutdown
        task_manager.current_state_error = SystemExit("Application shutting down.")


# API implementations
def set_app_config_internal(req: SetAppConfigRequest):
    config = app.getConfig()
    if req.update_branch is not None:
        config["update_branch"] = req.update_branch
    if req.render_devices is not None:
        update_render_devices_in_config(config, req.render_devices)
    if req.ui_open_browser_on_start is not None:
        if "ui" not in config:
            config["ui"] = {}
        config["ui"]["open_browser_on_start"] = req.ui_open_browser_on_start
    if req.listen_to_network is not None:
        if "net" not in config:
            config["net"] = {}
        config["net"]["listen_to_network"] = bool(req.listen_to_network)
    if req.listen_port is not None:
        if "net" not in config:
            config["net"] = {}
        config["net"]["listen_port"] = int(req.listen_port)

    config["use_v3_engine"] = req.use_v3_engine
    config["models_dir"] = req.models_dir

    for property, property_value in req.dict().items():
        if property_value is not None and property not in req.__fields__ and property not in PROTECTED_CONFIG_KEYS:
            config[property] = property_value

    try:
        app.setConfig(config)

        if req.render_devices:
            app.update_render_threads()

        return JSONResponse({"status": "OK"}, headers=NOCACHE_HEADERS)
    except Exception as e:
        log.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


def update_render_devices_in_config(config, render_devices):
    if render_devices not in ("cpu", "auto") and not render_devices.startswith("cuda:"):
        raise HTTPException(status_code=400, detail=f"Invalid render device requested: {render_devices}")

    if render_devices.startswith("cuda:"):
        render_devices = render_devices.split(",")

    config["render_devices"] = render_devices


def read_web_data_internal(key: str = None, **kwargs):
    if not key:  # /get without parameters, stable-diffusion easter egg.
        raise HTTPException(status_code=418, detail="StableDiffusion is drawing a teapot!")  # HTTP418 I'm a teapot
    elif key == "app_config":
        config = app.getConfig()

        if "models_dir" not in config:
            config["models_dir"] = app.MODELS_DIR

        return JSONResponse(config, headers=NOCACHE_HEADERS)
    elif key == "system_info":
        config = app.getConfig()

        output_dir = config.get("force_save_path", os.path.join(os.path.expanduser("~"), app.OUTPUT_DIRNAME))

        system_info = {
            "devices": task_manager.get_devices(),
            "hosts": app.getIPConfig(),
            "default_output_dir": output_dir,
            "enforce_output_dir": ("force_save_path" in config),
            "enforce_output_metadata": ("force_save_metadata" in config),
        }
        system_info["devices"]["config"] = config.get("render_devices", "auto")
        return JSONResponse(system_info, headers=NOCACHE_HEADERS)
    elif key == "models":
        scan_for_malicious = kwargs.get("scan_for_malicious", True)
        return JSONResponse(model_manager.getModels(scan_for_malicious), headers=NOCACHE_HEADERS)
    elif key == "modifiers":
        return JSONResponse(app.get_image_modifiers(), headers=NOCACHE_HEADERS)
    elif key == "ui_plugins":
        return JSONResponse(app.getUIPlugins(), headers=NOCACHE_HEADERS)
    else:
        raise HTTPException(status_code=404, detail=f"Request for unknown {key}")  # HTTP404 Not Found


def ping_internal(session_id: str = None):
    if task_manager.is_alive() <= 0:  # Check that render threads are alive.
        if task_manager.current_state_error:
            raise HTTPException(status_code=500, detail=str(task_manager.current_state_error))
        raise HTTPException(status_code=500, detail="Render thread is dead.")

    if task_manager.current_state_error and not isinstance(task_manager.current_state_error, StopAsyncIteration):
        raise HTTPException(status_code=500, detail=str(task_manager.current_state_error))

    # Alive
    response = {"status": str(task_manager.current_state)}

    if session_id:
        session = task_manager.get_cached_session(session_id, update_ttl=True)
        response["tasks"] = {id(t): t.status for t in session.tasks}

    response["devices"] = task_manager.get_devices()
    response["packages_installed"] = package_manager.get_installed_packages()
    response["packages_installing"] = package_manager.installing

    if cloudflare.address != None:
        response["cloudflare"] = cloudflare.address

    return JSONResponse(response, headers=NOCACHE_HEADERS)


def render_internal(req: dict):
    try:
        req = convert_legacy_render_req_to_new(req)

        #Generating random numbers in seed
        if (('seed' in req) and (req['seed'] == "random")):
            r1 = random.randint(1, 1000000000)
            r2 = random.randint(144, 244)-random.randint(24, 133)
            req['seed'] = r1+r2

        # Googletrans is a free and unlimited python library that implemented Google Translate API
        if (('prompt' in req) and (req.get("prompt", None))):
            t = Translator().detect(req['prompt'])
            if t.lang != "en":
                translator = Translator(service_urls=['translate.google.ru'])
                result = translator.translate(req['prompt'])
                req['prompt'] = result.text

        # separate out the request data into rendering and task-specific data
        render_req: GenerateImageRequest = GenerateImageRequest.parse_obj(req)
        task_data: RenderTaskData = RenderTaskData.parse_obj(req)
        models_data: ModelsData = ModelsData.parse_obj(req)
        output_format: OutputFormatData = OutputFormatData.parse_obj(req)
        save_data: SaveToDiskData = SaveToDiskData.parse_obj(req)

        # Overwrite user specified save path
        config = app.getConfig()
        if "force_save_path" in config:
            save_data.save_to_disk_path = config["force_save_path"]

        render_req.init_image_mask = req.get("mask")  # hack: will rename this in the HTTP API in a future revision

        app.save_to_config(
            models_data.model_paths.get("stable-diffusion"),
            models_data.model_paths.get("vae"),
            models_data.model_paths.get("hypernetwork"),
            task_data.vram_usage_level,
        )

        # enqueue the task
        task = RenderTask(render_req, task_data, models_data, output_format, save_data)
        return enqueue_task(task)
    except HTTPException as e:
        raise e
    except Exception as e:
        log.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


def filter_internal(req: dict):
    try:
        filter_req: FilterImageRequest = FilterImageRequest.parse_obj(req)
        task_data: TaskData = TaskData.parse_obj(req)
        models_data: ModelsData = ModelsData.parse_obj(req)
        output_format: OutputFormatData = OutputFormatData.parse_obj(req)
        save_data: SaveToDiskData = SaveToDiskData.parse_obj(req)

        # enqueue the task
        task = FilterTask(filter_req, task_data, models_data, output_format, save_data)
        return enqueue_task(task)
    except HTTPException as e:
        raise e
    except Exception as e:
        log.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


def enqueue_task(task):
    try:
        task_manager.enqueue_task(task)
        response = {
            "status": str(task_manager.current_state),
            "queue": len(task_manager.tasks_queue),
            "stream": f"/image/stream/{task.id}",
            "task": task.id,
        }
        return JSONResponse(response, headers=NOCACHE_HEADERS)
    except ChildProcessError as e:  # Render thread is dead
        raise HTTPException(status_code=500, detail=f"Rendering thread has died.")  # HTTP500 Internal Server Error
    except ConnectionRefusedError as e:  # Unstarted task pending limit reached, deny queueing too many.
        raise HTTPException(status_code=503, detail=str(e))  # HTTP503 Service Unavailable


def model_merge_internal(req: dict):
    try:
        from easydiffusion.utils.save_utils import filename_regex
        from sdkit.train import merge_models

        mergeReq: MergeRequest = MergeRequest.parse_obj(req)

        merge_models(
            model_manager.resolve_model_to_use(mergeReq.model0, "stable-diffusion"),
            model_manager.resolve_model_to_use(mergeReq.model1, "stable-diffusion"),
            mergeReq.ratio,
            os.path.join(
                app.MODELS_DIR,
                "stable-diffusion",
                filename_regex.sub("_", mergeReq.out_path),
            ),
            mergeReq.use_fp16,
        )
        return JSONResponse({"status": "OK"}, headers=NOCACHE_HEADERS)
    except Exception as e:
        log.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


def stream_internal(task_id: int):
    # TODO Move to WebSockets ??
    task = task_manager.get_cached_task(task_id, update_ttl=True)
    if not task:
        raise HTTPException(status_code=404, detail=f"Request {task_id} not found.")  # HTTP404 NotFound
    # if (id(task) != task_id): raise HTTPException(status_code=409, detail=f'Wrong task id received. Expected:{id(task)}, Received:{task_id}') # HTTP409 Conflict
    if task.buffer_queue.empty() and not task.lock.locked():
        if task.response:
            # log.info(f'Session {session_id} sending cached response')
            return JSONResponse(task.response, headers=NOCACHE_HEADERS)
        raise HTTPException(status_code=425, detail="Too Early, task not started yet.")  # HTTP425 Too Early
    # log.info(f'Session {session_id} opened live render stream {id(task.buffer_queue)}')
    return StreamingResponse(task.read_buffer_generator(), media_type="application/json")


def stop_internal(task: int):
    if not task:
        if (
            task_manager.current_state == task_manager.ServerStates.Online
            or task_manager.current_state == task_manager.ServerStates.Unavailable
        ):
            raise HTTPException(status_code=409, detail="Not currently running any tasks.")  # HTTP409 Conflict
        task_manager.current_state_error = StopAsyncIteration("")
        return {"OK"}
    task_id = task
    task = task_manager.get_cached_task(task_id, update_ttl=False)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} was not found.")  # HTTP404 Not Found
    if isinstance(task.error, StopAsyncIteration):
        raise HTTPException(status_code=409, detail=f"Task {task_id} is already stopped.")  # HTTP409 Conflict
    task.error = StopAsyncIteration(f"Task {task_id} stop requested.")
    return {"OK"}


def get_image_internal(task_id: int, img_id: int):
    task = task_manager.get_cached_task(task_id, update_ttl=True)
    if not task:
        raise HTTPException(status_code=410, detail=f"Task {task_id} could not be found.")  # HTTP404 NotFound
    if not task.temp_images[img_id]:
        raise HTTPException(status_code=425, detail="Too Early, task data is not available yet.")  # HTTP425 Too Early
    try:
        img_data = task.temp_images[img_id]
        img_data.seek(0)
        return StreamingResponse(img_data, media_type="image/jpeg")
    except KeyError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---- Cloudflare Tunnel ----
class CloudflareTunnel:
    def __init__(self):
        config = app.getConfig()
        self.urls = None
        self.port = config.get("net", {}).get("listen_port")

    def start(self):
        if self.port:
            self.urls = try_cloudflare(self.port)

    def stop(self):
        if self.urls:
            try_cloudflare.terminate(self.port)
            self.urls = None

    @property
    def address(self):
        if self.urls:
            return self.urls.tunnel
        else:
            return None


cloudflare = CloudflareTunnel()


def start_cloudflare_tunnel_internal(req: dict):
    try:
        cloudflare.start()
        log.info(f"- Started cloudflare tunnel. Using address: {cloudflare.address}")
        return JSONResponse({"address": cloudflare.address})
    except Exception as e:
        log.error(str(e))
        log.error(traceback.format_exc())
        return HTTPException(status_code=500, detail=str(e))


def stop_cloudflare_tunnel_internal(req: dict):
    try:
        cloudflare.stop()
    except Exception as e:
        log.error(str(e))
        log.error(traceback.format_exc())
        return HTTPException(status_code=500, detail=str(e))


def modify_package_internal(package_name: str, req: dict):
    try:
        cmd = req["command"]
        if cmd not in ("install", "uninstall"):
            raise RuntimeError(f"Unknown command: {cmd}")

        cmd = getattr(package_manager, cmd)
        cmd(package_name)

        return JSONResponse({"status": "OK"}, headers=NOCACHE_HEADERS)
    except Exception as e:
        log.error(str(e))
        log.error(traceback.format_exc())
        return HTTPException(status_code=500, detail=str(e))


def get_sha256_internal(obj_path):
    from easydiffusion.utils import sha256sum

    path = obj_path.split("/")
    type = path.pop(0)

    try:
        model_path = model_manager.resolve_model_to_use("/".join(path), type)
    except Exception as e:
        log.error(str(e))
        log.error(traceback.format_exc())

        return HTTPException(status_code=404)
    try:
        digest = sha256sum(model_path)
        return {"digest": digest}
    except Exception as e:
        log.error(str(e))
        log.error(traceback.format_exc())
        return HTTPException(status_code=500, detail=str(e))
