"""从索引构建 Treemap 用的两层目录树（在后台线程调用）。"""
from __future__ import annotations

import os

from core.index import Index
from .widgets.treemap import TreeNode


def _name(path: str) -> str:
    stripped = path.rstrip("\\")
    return os.path.basename(stripped) or path


def build_tree(index: Index, path: str, child_limit: int = 48, grand_parents: int = 28,
               grand_limit: int = 36) -> TreeNode | None:
    info = index.dir_info(path)
    if not info:
        return None
    root = TreeNode(path, _name(path), int(info["size"]), int(info["files"] or 0))
    kids = index.folder_children(path, child_limit)
    totals = index.children_totals([path] + [k["path"] for k in kids[:grand_parents]])
    nodes: list[TreeNode] = []
    for i, kid in enumerate(kids):
        node = TreeNode(kid["path"], _name(kid["path"]), int(kid["size"]), int(kid["files"] or 0))
        if i < grand_parents and node.size > 0:
            grand = index.folder_children(kid["path"], grand_limit)
            node.children = [
                TreeNode(g["path"], _name(g["path"]), int(g["size"]), int(g["files"] or 0)) for g in grand
            ]
            sub_total = totals.get(kid["path"], 0)
            shown = sum(g.size for g in node.children)
            if sub_total - shown > 0:
                node.children.append(TreeNode(kid["path"], "其它子目录", sub_total - shown, kind="other"))
            if node.size - sub_total > 0:
                node.children.append(TreeNode(kid["path"], "（文件）", node.size - sub_total, kind="files"))
            node.children.sort(key=lambda n: -n.size)
        nodes.append(node)
    all_kids = totals.get(path, 0)
    shown = sum(n.size for n in nodes)
    if all_kids - shown > 0:
        nodes.append(TreeNode(path, "其它子目录", all_kids - shown, kind="other"))
    if root.size - all_kids > 0:
        nodes.append(TreeNode(path, "（本层文件）", root.size - all_kids, kind="files"))
    nodes.sort(key=lambda n: -n.size)
    root.children = nodes
    return root
