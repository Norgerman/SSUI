# scheduler.py
import asyncio
import json
import logging
import multiprocessing
import threading
from typing import Dict, List, Optional, Any, Union
from datetime import datetime
from .model import Task, TaskStatus, ExecutorInfo, ExecutorRegister, RegisterResponse, UpdateStatus, TaskResult, ExeMessage
import time
import websockets
from .executor_main import main
import traceback

logger = logging.getLogger("TaskScheduler")

class TaskScheduler:
    """异步任务调度器，用于管理执行器连接和任务分配"""
    
    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.executors: Dict[str, ExecutorInfo] = {}
        self.executor_websockets: Dict[str, websockets.ClientConnection] = {}
        self.task_queue = asyncio.PriorityQueue()
        self.lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self.loop = None
        self.thread = None

    def start(self):
        self.loop = asyncio.new_event_loop()
        
        """启动调度器"""
        self.thread = threading.Thread(target=lambda: self.loop.run_until_complete(self._start_server()), daemon=True)
        self.thread.start()

        logger.info("任务调度器已启动")
        # 为executor创建新进程
        multiprocessing.Process(target=main, daemon=True).start()
    
    async def _start_server(self):
        async with websockets.serve(self.handle_executor_connection, "localhost", 5000):
            await asyncio.Future()

    async def _stop(self):
        """停止调度器"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            
        # 关闭所有WebSocket连接
        for ws in self.executor_websockets.values():
            await ws.close()
            
        self.executor_websockets.clear()
        self.executors.clear()
        logger.info("任务调度器已停止")
        
    def stop(self):
        """停止调度器"""
        self.thread.join(0)
        
    async def handle_executor_connection(self, websocket: websockets.ClientConnection):
        """处理执行器WebSocket连接"""
        print(websocket.remote_address)
        host, port, _, _ = websocket.remote_address
        executor_id = f"{host}:{port}"

        try:
            # 如果执行器已存在，关闭旧连接
            if executor_id in self.executor_websockets:
                old_ws = self.executor_websockets[executor_id]
                await old_ws.close()
            
            # 创建执行器信息
            executor = ExecutorInfo(
                executor_id=executor_id,
                host=host,
                port=port,
            )
            
            self.executors[executor_id] = executor
            self.executor_websockets[executor_id] = websocket

            # 等待注册消息
            async for message in websocket:
                try:
                    # 使用ExeMessage解析消息
                    exe_message = ExeMessage.validate_json(message)
                    await self._process_executor_message(executor_id, exe_message)
                except Exception as e:
                    logger.error(f"解析消息失败: {e}")
                
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"执行器 {executor_id} 已断开连接")
        except Exception as e:
            # 打印异常调用栈
            logger.error(f"执行器连接处理异常详情:\n{traceback.format_exc()}")
        finally:
            # 清理连接
            if executor_id in self.executor_websockets:
                del self.executor_websockets[executor_id]
            if executor_id in self.executors:
                self.executors[executor_id].is_active = False
                
    def add_task(self, task: Task) -> str:
        """添加新任务"""
        self.tasks[task.task_id] = task
        # 尝试立即分配任务
        if self._try_assign_task(task):
            logger.info(f"任务 {task.task_id} 已立即分配给执行器")
        else:
            # 如果无法立即分配，加入队列
            self.loop.call_soon_threadsafe(self.task_queue.put_nowait, (-task.priority, task.task_id))
            logger.info(f"任务 {task.task_id} 已加入队列")
        return task.task_id
            
    def get_task(self, task_id: str) -> Optional[Task]:
        """获取任务信息"""
        return self.tasks.get(task_id)
            
    def get_all_tasks(self) -> List[Task]:
        """获取所有任务"""
        return list(self.tasks.values())
            
    def get_all_executors(self) -> List[ExecutorInfo]:
        """获取所有执行器信息"""
        return list(self.executors.values())
            
    def _try_assign_task(self, task: Task) -> bool:
        """尝试将任务分配给可用的执行器"""
        if task.status != TaskStatus.PENDING:
            return False
            
        # 查找可用的执行器
        available_executor = None
        available_ws = None
        
        for executor_id, executor in self.executors.items():
            if (executor.is_active and 
                executor.current_tasks < executor.max_tasks and 
                (datetime.now() - executor.last_heartbeat).seconds < 30):
                available_executor = executor
                available_ws = self.executor_websockets.get(executor_id)
                break
                
        if not available_executor or not available_ws:
            return False
            
        try:
            # 更新任务状态
            task.status = TaskStatus.RUNNING
            task.started_at = str(datetime.now())
            task.executor_id = available_executor.executor_id
            
            # 更新执行器状态
            available_executor.current_tasks += 1
            
            # 发送任务给执行器
            logger.info(f"发送任务给执行器: {task.model_dump_json()}")
            asyncio.create_task(available_ws.send(task.model_dump_json()))
            
            return True
            
        except Exception as e:
            logger.error(f"分配任务时出错:\n{traceback.format_exc()}")
            # 恢复状态
            task.status = TaskStatus.PENDING
            task.started_at = None
            task.executor_id = None
            available_executor.current_tasks = max(0, available_executor.current_tasks - 1)
            return False
            
    async def _process_executor_message(self, executor_id: str, message: Union[ExecutorRegister, RegisterResponse, UpdateStatus, TaskResult]):
        """处理来自执行器的消息"""
        async with self.lock:
            if executor_id in self.executors:
                executor = self.executors[executor_id]
                executor.last_heartbeat = datetime.now()
                
                if isinstance(message, ExecutorRegister):
                    # 发送注册成功响应
                    register_response = RegisterResponse(
                        status="success",
                        message="注册成功"
                    )
                    await self.executor_websockets[executor_id].send(register_response.model_dump_json())
                    
                    logger.info(f"执行器 {executor_id} 已注册")
                    if self.task_queue.qsize() > 0:
                        priority, task_id = self.task_queue.get_nowait()
                        logger.info(f"尝试分配任务 {task_id}")
                        self._try_assign_task(self.tasks[task_id])

                elif isinstance(message, UpdateStatus):
                    # 处理状态更新
                    task_id = message.task_id
                    if task_id in self.tasks:
                        task = self.tasks[task_id]
                        task.status = message.status
                        
                elif isinstance(message, TaskResult):
                    task_id = message.task_id
                    if task_id in self.tasks:
                        task = self.tasks[task_id]
                        task.status = message.status
                        
                        if message.status == TaskStatus.COMPLETED:
                            task.result = message.result
                            task.completed_at = str(datetime.now())
                            task.executor_id = None
                            executor.current_tasks = max(0, executor.current_tasks - 1)
                            logger.info(f"任务 {task_id} 已完成")
                        elif message.status == TaskStatus.FAILED:
                            task.error = message.error
                            task.completed_at = str(datetime.now())
                            task.executor_id = None
                            executor.current_tasks = max(0, executor.current_tasks - 1)
                            logger.info(f"任务 {task_id} 失败")
                        
    async def _check_heartbeats(self):
        """检查执行器心跳"""
        while True:
            try:
                async with self.lock:
                    current_time = datetime.now()
                    for executor_id, executor in list(self.executors.items()):
                        if (current_time - executor.last_heartbeat).seconds > 60:
                            executor.is_active = False
                            logger.warning(f"执行器 {executor_id} 心跳超时")
                            
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"检查心跳时出错: {e}")
                await asyncio.sleep(10)
                
    async def wait_until_finished(self, task_id: Optional[str] = None, timeout: Optional[float] = None) -> Optional[Union[Task, List[Task]]]:
        """
        等待任务完成并返回任务对象
        
        Args:
            task_id: 要等待的任务ID，如果为None则等待所有任务完成
            timeout: 超时时间（秒），如果为None则无限等待
            
        Returns:
            如果指定了task_id，则返回完成的任务对象，如果超时则返回None
            如果task_id为None，则返回所有完成的任务列表，如果超时则返回None
        """
        start_time = time.time()
        
        while True:
            # 检查是否超时
            if timeout is not None and time.time() - start_time > timeout:
                logger.warning(f"等待任务完成超时")
                return None
                
            # 获取任务状态
            async with self.lock:
                if task_id is None:
                    # 等待所有任务完成
                    all_tasks = list(self.tasks.values())
                    if not all_tasks:
                        logger.info("没有任务需要等待")
                        return []
                        
                    # 检查是否所有任务都已完成
                    all_completed = all(
                        task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
                        for task in all_tasks
                    )
                    
                    if all_completed:
                        return all_tasks
                else:
                    # 等待单个任务完成
                    task = self.tasks.get(task_id)
                    if not task:
                        logger.warning(f"任务 {task_id} 不存在")
                        return None
                        
                    # 检查任务是否已完成
                    if task.status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]:
                        return task
                    
            # 等待一段时间后再次检查
            await asyncio.sleep(0.5)