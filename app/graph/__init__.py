"""LangGraph 研究图模块。

用法::

    from app.graph.builder import build_graph
    from app.config import get_settings

    graph = build_graph(get_settings())

"""

from app.graph.builder import build_graph

__all__ = ["build_graph"]
