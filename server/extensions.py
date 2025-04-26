import os
import re
import sys
from types import ModuleType
import yaml
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Dict, Optional
from .opener_service import FileOpenerManager
class ExtensionServerConfig(BaseModel):
    venv: str = Field(default="shared", description="The virtual environment to use for the extension")
    dependencies: list[str] = Field(default=[], description="The dependencies to install for the extension")
    main: str = Field(default="extension.py", description="The main file to run for the extension")

class ExtensionWebUIConfig(BaseModel):
    dist: str = Field(default="dist", description="The dist directory for the extension")
    mount: str = Field(default="", description="The mount directory for the dist path")
    file_opener: Optional[list[Dict[str, str]]] = Field(default=None, description="The file openers to extend")

class Extension(BaseModel):
    name: str = Field(description="The name of the extension")
    path: str = Field(description="The path to the extension")
    version: str = Field(description="The version of the extension")
    server: ExtensionServerConfig = Field(default=ExtensionServerConfig(), description="The server configuration for the extension")
    web_ui: ExtensionWebUIConfig = Field(default=ExtensionWebUIConfig(), description="The web UI configuration for the extension")



class ExtensionManager:
    """
    ExtensionManager 类用于管理服务器扩展。
    
    该类采用单例模式设计，负责扩展的检测、加载和管理。它可以：
    - 从指定目录检测可用的扩展
    - 加载扩展的配置信息
    - 加载扩展的Python脚本
    - 为扩展设置静态文件API
    
    扩展通过ssextension.yaml文件进行配置，该文件定义了扩展的名称、版本、
    服务器配置和Web UI配置等信息。
    """

    @classmethod
    def instance(cls):
        if not hasattr(cls, "_instance"):
            cls._instance = cls()
        return cls._instance

    def __init__(self, path: Optional[str] = None):
        self.modules: dict[str, Optional[ModuleType]] = {}
        self.extensions: dict[str, Extension] = {}
        if path is None:
            path = os.path.join(os.path.dirname(__file__), "..", "extensions")
            path = os.path.normpath(path)
        self.path = path

    def loadExtension(self, yaml_path: str, dir: str):
        with open(yaml_path, "r") as f:
            yaml_data = yaml.load(f, Loader=yaml.FullLoader)
            yaml_data["path"] = os.path.join(self.path, dir)
            if "name" not in yaml_data:
                yaml_data["name"] = dir
            if yaml_data["name"] not in self.extensions:
                extension = Extension(
                    name=yaml_data["name"],
                    path=yaml_data["path"],
                    version=yaml_data["version"],
                    server=ExtensionServerConfig(**yaml_data.get("server", {})),
                    web_ui=ExtensionWebUIConfig(**yaml_data.get("web_ui", {}))
                )
                self.extensions[yaml_data["name"]] = extension

    def detectExtensions(self, app: FastAPI):
        for dir in os.listdir(self.path):
            yaml_path = os.path.join(self.path, dir, "ssextension.yaml")
            if os.path.exists(yaml_path):
                self.loadExtension(yaml_path, dir)
                        
        self.loadFileOpener()
        self.loadPythonScripts(app)
        self.setFileAPIforExtension(app)
                    
    def getExtensions(self, name: str) -> Extension:
        return self.extensions[name]
    
    def loadPythonScripts(self, app: FastAPI):
        for name, extension in self.extensions.items():
            if extension.server and extension.server.main:
                print(f"Loading {name} from {extension.server.main}")
                script_path = os.path.join(extension.path, extension.server.main)
                dir_path = os.path.dirname(script_path)
                sys.path.append(dir_path)
                print(f"Appending {dir_path} to sys.path")
                import importlib.util
                try:
                    spec = importlib.util.spec_from_file_location(name, script_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self.modules[name] = module  # 保存模块

                    router = module.app
                    app.include_router(router, prefix=f"/extension/{name}")  # 添加路由
                except Exception as e:
                    print(e)
                    self.modules[name] = None

    def setFileAPIforExtension(self, app: FastAPI):
        for name, extension in self.extensions.items():
            if extension.web_ui and extension.web_ui.dist:
                mount = extension.web_ui.mount
                dist_path = os.path.normpath(os.path.join(extension.path, extension.web_ui.dist))
                if os.path.exists(dist_path):
                    print(f"Setting static files for {name} at {dist_path}")
                    app.mount(f"/extension/{name}/{mount}", StaticFiles(directory=dist_path), name=name)

    def loadFileOpener(self):
        def parseFileOpener(file_opener: str):
            match = re.match(r"^([^()]+)\((.*?)\)([^()]*)$", file_opener)
            if match:
                url_path = match.group(1)
                pattern = match.group(2)
                url_rest = match.group(3)
                return url_path, pattern, url_rest
            return None

        for name, extension in self.extensions.items():
            if extension.web_ui and extension.web_ui.file_opener:
                for dic in extension.web_ui.file_opener:
                    for opener_name, file_opener in dic.items():
                        pattern = parseFileOpener(file_opener)
                        if pattern:
                            if pattern[1].startswith("*"):
                                file_extension = pattern[1][1:]
                            else:
                                file_extension = pattern[1]
                            FileOpenerManager.instance().register_opener(
                                opener_name,
                                file_extension,
                                f'/extension/{name}'+pattern[0],
                                pattern[2]
                            )
                        
