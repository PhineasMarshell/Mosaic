"""T10 回归：app.* 命名空间的日志必须真正输出，不能因 handler 挂错命名空间而静默丢失。"""

import logging

from app.logging_config import setup_logging


def test_app_namespace_info_emitted(capsys):
    # 生产默认 propagate=False：handler 直接挂在 app 命名空间上。
    setup_logging(level="DEBUG", json_format=False, propagate=False)
    logging.getLogger("app.graph.nodes.critic").info("critic-info-marker")

    out = capsys.readouterr().out
    assert "critic-info-marker" in out


def test_mosaic_namespace_info_emitted(capsys):
    setup_logging(level="DEBUG", json_format=False, propagate=False)
    logging.getLogger("mosaic.app").info("mosaic-info-marker")

    out = capsys.readouterr().out
    assert "mosaic-info-marker" in out


def test_app_info_visible_to_caplog(caplog):
    # 与 test_ask_endpoint 一致：propagate=True 时 caplog（挂在 root）能抓到 app.* INFO。
    setup_logging(level="DEBUG", json_format=False, propagate=True)
    logging.getLogger("app.graph.nodes.critic").info("caplog-marker")

    assert any("caplog-marker" in record.getMessage() for record in caplog.records)
