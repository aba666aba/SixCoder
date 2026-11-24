"""Celery 应用（**由引擎生成**）。

这是 microservice 栈的**识别 marker**（manifest 的 detect_markers 认 celery_app.py）。
所以这个文件必须存在——删了的话引擎就不认这是多服务系统了。
"""
from __future__ import annotations

from celery import Celery

app = Celery(
    "sixcoder",
    broker="redis://127.0.0.1:6379/0",
    backend="redis://127.0.0.1:6379/1",
    include=["app.tasks"],
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
)
