"""GitHub Gist 作为 JSON 存储的通用能力。

零业务依赖，仅依赖 requests；可被其他项目单独拷走复用：
    from gist_store import GistStore
    store = GistStore(gist_id, token)
    files = store.fetch()                 # {文件名: 内容字符串}
    data = store.get_json("state.json", default={})
    store.update("state.json", {"a": 1})  # 写回单个文件

适用场景：以 GitHub Gist 保存跨运行的配置、凭证、状态（常见于 GitHub Actions 定时任务）。
"""
import json
import logging

import requests

_LOGGER = logging.getLogger(__name__)

_API = "https://api.github.com/gists/"


def _make_log(log):
    """统一日志入口：外部注入单参 log(msg) 则用之，否则回退标准库 logging。"""
    if log is not None:
        return log

    def _default(msg):
        _LOGGER.info(msg)

    return _default


class GistStore:
    """把一个 Gist 当作「文件名 -> 内容」的 JSON 存储读写。"""

    def __init__(self, gist_id, token, *, verify=True, description="update", log=None):
        self.gist_id = gist_id
        self.token = token
        self.verify = verify
        self.description = description
        self._log = _make_log(log)

    def _headers(self):
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self.token,
        }

    def fetch(self, log=True):
        """一次 GET 拉取整个 Gist，返回 {文件名: 内容字符串}；log=False 静默（供高频轮询）。"""
        rsp = requests.get(_API + self.gist_id, headers=self._headers(), verify=self.verify)
        if rsp.status_code == 404:
            raise Exception("gist id 错误")
        if rsp.status_code in (401, 403):
            raise Exception("github TOKEN 错误")
        if log:
            self._log(f"Gist 请求成功，HTTP {rsp.status_code}")
        files = rsp.json().get("files", {})
        result = {name: f.get("content") for name, f in files.items()}
        if log:
            self._log(f"Gist 包含文件：{list(result.keys())}")
        return result

    def get_text(self, name, default=None):
        """取单个文件的原始文本，不存在返回 default。"""
        content = self.fetch().get(name)
        return default if content is None else content

    def get_json(self, name, default=None):
        """取单个文件并按 JSON 解析，不存在返回 default。"""
        content = self.fetch().get(name)
        return default if content is None else json.loads(content)

    def update(self, name, data):
        """写回单个文件：data 为 str 原样写，否则序列化为 JSON。"""
        content = data if isinstance(data, str) else json.dumps(data, indent="  ", ensure_ascii=False)
        self._log(f"正在更新 Gist 文件：{name}")
        rsp = requests.post(
            _API + self.gist_id,
            json={"description": self.description, "files": {name: {"content": content}}},
            headers=self._headers(),
            verify=self.verify,
        )
        if rsp.status_code == 404:
            raise Exception("gist id 错误")
        if rsp.status_code == 422:
            raise Exception("github TOKEN 错误")
        self._log(f"Gist 更新成功，HTTP {rsp.status_code}")
