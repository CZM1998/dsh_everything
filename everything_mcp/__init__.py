"""Everything MCP 服务器。

把本机 voidtools Everything 的毫秒级文件索引通过 MCP stdio 暴露给 Agent，
用正则/通配符查询替代慢速递归目录扫描。

模块结构：
- ``config``        环境变量配置
- ``http_channel``  Everything 内置 HTTP Server 查询通道（urllib）
- ``winipc``        Windows 进程/窗口探测与 exe 版本读取（ctypes，仅诊断用途）
- ``client``        通道选择、自动拉起 Everything、4 个高层操作
- ``server``        FastMCP stdio 服务器与工具定义
"""

__version__ = "0.1.0"
