import os
import sys
import logging
import json
import asyncio
import time
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from mcp import types
from typing import Any
import mcp.server.stdio
import websockets
from websockets.server import WebSocketServerProtocol

HOST = '127.0.0.1'
PORT = 8766

# reconfigure UnicodeEncodeError prone default (i.e. windows-1252) to utf-8
if sys.platform == "win32" and os.environ.get('PYTHONIOENCODING') is None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 配置日志
# log_file = os.path.expanduser(f"./logs/{time.strftime('%Y-%m-%d')}.log")
# logging.basicConfig(
#     level=logging.DEBUG,
#     format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
#     handlers=[
#         logging.FileHandler(log_file),
#         logging.StreamHandler()
#     ]
# )
logger = logging.getLogger('figma-mcp-server')
logger.info("Starting Figma MCP Server")

# 存储所有连接的 WebSocket 客户端
connected_clients = set()

# 存储命令ID和对应的Future
command_futures: dict[str, asyncio.Future] = {}

async def handle_websocket(websocket: WebSocketServerProtocol):
    try:
        # 添加新的客户端连接
        connected_clients.add(websocket)
        logger.info(f"New WebSocket client connected. Total clients: {len(connected_clients)}")
        
        async for message in websocket:
            logger.debug(f"Received WebSocket message: {message}")
            # 处理接收到的消息
            try:
                data = json.loads(message)
                logger.debug(f"Parsed WebSocket message: {data}")
                
                # 直接处理响应数据
                command_id = data.get("id")
                logger.debug(f"Processing response data: {data} for command {command_id}")

                logger.debug(f"Current command_futures: {command_futures}")
                
                if command_id and command_id in command_futures:
                    future = command_futures[command_id]
                    logger.info(f"Found future for command {command_id}")
                    
                    # 处理响应结果
                    response_data = data.get("data", {})
                    if response_data.get("success"):
                        result = response_data.get("result")
                        type = response_data.get("type")
                        logger.debug(f"Command result: {result}")
                        if type == "image":
                            future.set_result([types.ImageContent(type="image", mimeType="image/png", data=result.get('data'))])
                        else:
                            text = repr(result) if isinstance(result, (dict, list)) else "{}".format(result)
                            future.set_result([types.TextContent(type="text", text=text)])
                    else:
                        error = response_data.get("error", "Unknown error")
                        logger.error(f"Command {command_id} failed: {error}")
                        future.set_exception(Exception(error))
                    
                    del command_futures[command_id]  # 清理Future
                    logger.debug(f"Cleaned up future for command {command_id}")
                    
                # 发送确认消息
                logger.debug(f"Sending confirmation message for command {command_id}")
                response = {"type": "received", "message": "Success"}
                await websocket.send(json.dumps(response))
            except json.JSONDecodeError:
                response = {"type": "error", "message": "Invalid JSON"}
                await websocket.send(json.dumps(response))
    except websockets.exceptions.ConnectionClosed:
        logger.info("WebSocket connection closed")
    finally:
        # 移除断开连接的客户端
        connected_clients.remove(websocket)
        logger.info(f"Client disconnected. Total clients: {len(connected_clients)}")
        
        # 处理所有未完成的Future
        for future in command_futures.values():
            if not future.done():
                future.set_exception(ConnectionError("WebSocket connection closed"))

async def start_websocket_server():
    async with websockets.serve(handle_websocket, HOST, PORT):
        logger.info(f"WebSocket server started on ws://{HOST}:{PORT}")
        await asyncio.Future()  # 保持服务器运行

async def main():
    server = Server("figma-mcp-server")
    
    # 启动 WebSocket 服务器
    websocket_task = asyncio.create_task(start_websocket_server())

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """List available tools"""
        return [
            types.Tool(
                name="figma_get_pages",
                description="Get all pages of the document in Figma",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                }
            ),
            types.Tool(
                name="figma_get_root_layers_of_page",
                description="Get all layers of a specific page in Figma",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "pageId": {
                            "type": "string",
                            "description": "ID of the page to get layers from"
                        }
                    },
                    "required": ["pageId"],
                }
            ),
            types.Tool(
                name="figma_get_selection",
                description="Get the current selection in Figma",
                inputSchema={
                    "type": "object",
                    "properties": {
                    },
                    "required": [],
                }
            ),
            types.Tool(
                name="figma_get_node_children",
                description="Get all components of the Node in Figma",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "nodeId": {
                            "type": "string",
                            "description": "ID of the node to get components from"
                        }
                    },
                    "required": ["nodeId"],
                }
            ),
            types.Tool(
                name="figma_export_node",
                description="Export a Node from Figma",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "nodeId": {
                            "type": "string",
                            "description": "ID of the node to export"
                        }
                    },
                    "required": ["nodeId"],
                }
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ):
        """Handle tool execution requests"""
        logger.info(f"Received call tool request: {name} with args: {arguments}")
        
        if not connected_clients:
            raise RuntimeError("No WebSocket clients connected")
        
        # 创建命令ID和Future
        command_id = f"{name}_{int(time.time() * 1000)}"
        future = asyncio.Future()
        command_futures[command_id] = future
        logger.debug(f"Created future for command {command_id}")
        logger.debug(f"Current command_futures: {command_futures}")
        
        try:
            # 创建要发送给 WebSocket 客户端的消息
            ws_message = {
                "type": "command",
                "command": name.replace("figma_", ""),
                "params": arguments,
                "id": command_id
            }
            
            # 向所有连接的 WebSocket 客户端广播消息
            message_json = json.dumps(ws_message)
            logger.debug(f"Sending WebSocket message: {message_json}")
            
            websockets_tasks = []
            for websocket in connected_clients:
                try:
                    task = asyncio.create_task(websocket.send(message_json))
                    websockets_tasks.append(task)
                    logger.debug(f"Created send task for client {id(websocket)}")
                except websockets.exceptions.ConnectionClosed:
                    logger.warning(f"Client {id(websocket)} connection closed")
                    continue
            
            if websockets_tasks:
                logger.debug(f"Waiting for {len(websockets_tasks)} send tasks to complete")
                await asyncio.gather(*websockets_tasks)
                logger.debug("All send tasks completed")
                logger.debug(f"After sending, command_futures: {command_futures}")
                
                # 等待结果，设置60秒超时
                try:
                    logger.debug(f"Waiting for response for command {id}")
                    result = await asyncio.wait_for(future, timeout=60.0)
                    logger.info(f"Command {name} completed with result: {result}")
                    return result
                except asyncio.TimeoutError:
                    logger.error(f"Command {name} timed out waiting for response")
                    raise TimeoutError(f"Command {name} timed out waiting for response")
            else:
                logger.error("No WebSocket clients connected to send command to")
                raise RuntimeError("No WebSocket clients connected")
        except Exception as e:
            logger.error(f"Error handling command {name}: {e}", exc_info=True)
            raise
        finally:
            # 清理Future
            if command_id in command_futures:
                logger.debug(f"Cleaning up future for command {command_id}")
                del command_futures[command_id]
        

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("Server running with stdio transport")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="figma",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )