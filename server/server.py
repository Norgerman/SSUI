import asyncio
from typing import Dict, Any
import uuid
from fastapi import Body, FastAPI, Request, Response, WebSocket, UploadFile, File
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
import sys
import os
import json
from server.opener_service import FileOpenerManager
from ss_executor.scheduler import TaskScheduler
from contextlib import asynccontextmanager

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from ss_executor import search_project_root
from .extensions import ExtensionManager

from server.models import ModelInfo, ScanModelsRequest
from server.config_service import ConfigService
from server.model_service import ModelService
from server.script_service import ScriptService
from server.websocket_service import WebSocketService

# 资源目录
resources_dir: str = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "resources")
)
settings_path: str = os.path.join(resources_dir, "ssui_config.json")

# 创建服务
config_service = ConfigService(settings_path)
model_service = ModelService(resources_dir)
scheduler = TaskScheduler()
script_service = ScriptService(scheduler)
websocket_service = WebSocketService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 检测扩展
    ExtensionManager.instance().detectExtensions(app)
    print("检测扩展完成")
    await scheduler.start()
    yield
    # 关闭所有连接
    print("closing scheduler and all websocket connections.")
    await scheduler.stop()
    websocket_service.stop()


app = FastAPI(lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/config/")
async def config(config: Dict[str, Any]):
    return config_service.update_config(config)

class ScanModelsRequest(BaseModel):
    scan_dir: str = Field(description="The directory to scan for models")

# API Use: /desktop/src/providers/TauriModelsProvider.ts
@app.post("/config/scan_models/{client_id}")
async def scan_models(client_id: str, request: ScanModelsRequest):
    scan_dir = os.path.normpath(request.scan_dir)
    print("scan_models", client_id, scan_dir)
    if not os.path.exists(scan_dir):
        return {"error": "Scan directory not found"}
    request_uuid = str(uuid.uuid4())
    
    return JSONResponse(content=jsonable_encoder({
        "type": "start",
        "request_uuid": request_uuid,
        "callbacks": ["model_found"],
    }), background=BackgroundTask(model_service.scan_models, 
        scan_dir=scan_dir,
        client_id=client_id,
        request_uuid=request_uuid,
        callback=websocket_service.send_callback,
        finish_callback=websocket_service.send_finish)
    )


# API Use: /desktop/src/providers/TauriModelsProvider.ts
@app.post("/config/install_model")
async def install_model(
    model_path: str = Body(..., embed=True),
    create_softlink: bool = Body(False, embed=True),
):
    result = await model_service.install_model(model_path, create_softlink)
    if "type" in result and result["type"] == "success":
        model_info = ModelInfo(**result)
        config_service.add_installed_model(model_info)
        
    return result

@app.get("/config/opener/{file_extension}")
async def opener(file_extension: str):
    return FileOpenerManager.instance().get_opener(file_extension)

@app.get("/config/opener")
async def opener():
    return FileOpenerManager.instance().get_all_openers()


@app.get("/api/version")
async def version():
    return script_service.get_torch_version()


@app.get("/api/device")
async def device():
    return script_service.get_device_info()


@app.get("/api/script")
async def script(script_path: str):
    result = {}
    result["functions"] = script_service.get_script_functions(script_path)
    result["root_path"] = search_project_root(script_path)
    return result


@app.get("/api/model")
async def model(model_path: str):
    model_path = os.path.normpath(model_path)
    meta_path = model_path + ".meta"
    data = json.load(open(meta_path, "r"))
    data["path"] = model_path
    return data


@app.get("/api/available_models")
async def available_models():
    return config_service.get_installed_models()


# 下面是执行器相关的API

@app.post("/api/prepare")
async def prepare(script_path: str, callable: str):
    return await script_service.prepare_script(script_path, callable)


@app.post("/api/execute")
async def execute(script_path: str, callable: str, params: Dict[str, Any], details: Dict[str, Any]):
    return await script_service.execute_script(script_path, callable, params, details)

@app.get("/file/root_path")
async def root_path(script_path: str):
    return search_project_root(script_path)

@app.get("/files/{file_type}")
async def files(file_type: str, script_path: str):
    ext_name_map = {
        "image": ("png", "jpg", "jpeg", "bmp"),
        "video": ("mp4", "avi", "mov", "mkv"),
        "audio": ("mp3", "wav", "m4a", "ogg"),
        "3dmodel": ("obj", "fbx", "glb", "gltf"),
        "script": ("py")
    }

    project_root = search_project_root(script_path)
    if project_root is None:
        return {"error": "Project root not found"}

    # TODO: 也许对于日后大目录来说，需要考虑性能问题，但我决定遇到了再说
    result_files = []
    for root, dirs, filenames in os.walk(project_root):
        for filename in filenames:
            if filename.endswith(ext_name_map[file_type]):
                result_files.append(os.path.join(root, filename))
    return result_files

@app.post("/files/upload")
async def upload_file(script_path: str, file: UploadFile = File(...)):
    project_root = search_project_root(script_path)
    if project_root is None:
        return {"error": "Project root not found"}
    
    # 创建input文件夹
    input_dir = os.path.join(project_root, "input")
    if not os.path.exists(input_dir):
        os.makedirs(input_dir)
    
    # 保存上传的文件
    file_path = os.path.join(input_dir, file.filename)
    try:
        contents = await file.read()
        with open(file_path, "wb") as f:
            f.write(contents)
        return {"success": True, "path": file_path}
    except Exception as e:
        return {"error": str(e)}

@app.get("/file")
async def file(path: str):
    print("access file: ", path)
    if os.path.exists(path):
        if path.endswith(".png"):
            return FileResponse(path, media_type="image/png")
        elif path.endswith(".jpg") or path.endswith(".jpeg"):
            return FileResponse(path, media_type="image/jpeg")
        else:
            return FileResponse(path)
    return None


@app.get("/api/extensions")
async def extensions():
    return ExtensionManager.instance().extensions

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    try:
        client_id = await websocket_service.connect(websocket)
        while websocket_service.is_running:
            await websocket.receive_text()  # 保持连接
    except Exception as e:
        print("client disconnected: ", e)
        websocket_service.disconnect(client_id)



# 对于静态数据的请求，使用文件资源管理器
settings = config_service.get_settings()
if settings.host_web_ui:
    @app.get("/functional_ui/", response_class=RedirectResponse)
    async def root(request: Request):
        query_string = request.url.query
        redirect_url = "/functional_ui/index.html"
        if query_string:
            redirect_url += f"?{query_string}"
        return RedirectResponse(url=redirect_url)

    app.mount("/functional_ui/", StaticFiles(directory=settings.host_web_ui), name="static")
    print("mount functional_ui", settings.host_web_ui)

FileOpenerManager.instance().register_opener("FunctionalUI", ".py", "/functional_ui/?path=")
FileOpenerManager.instance().register_opener("ProjectSettings", "ssproject.yaml", "/functional_ui/?view=project_settings&path=")
FileOpenerManager.instance().register_opener("ImagePreview", ".png", "/functional_ui/?view=image_preview&path=")

