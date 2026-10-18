# -*- coding: utf-8 -*-
"""douyin_sync / douyin_downloader 的离线单元测试（不触网、不写远端）。"""
import json
import os
import tempfile
import unittest

import douyin_sync as ds
from douyin_downloader import DouyinDownloader


class TitleTagsTest(unittest.TestCase):
    def test_title_strips_emoji_and_hashtags(self):
        self.assertEqual(ds._title_from_desc("28岁啦🎂亲爱的朋友们"), "28岁啦亲爱的朋友们")
        self.assertEqual(ds._title_from_desc("第一行\n第二行 #标签"), "第一行")
        self.assertEqual(ds._title_from_desc("Video Title #tag"), "Video Title")

    def test_title_fallbacks(self):
        self.assertEqual(ds._title_from_desc(""), "抖音视频")
        self.assertEqual(ds._title_from_desc("  \n  "), "抖音视频")
        self.assertEqual(ds._title_from_desc("#tag1 #tag2\n正文"), "抖音视频")

    def test_tags_merge_and_dedupe(self):
        tags = ds._tags_from_desc("正文 #a #a #b,", "b,固定")
        self.assertEqual(tags, "a,b,固定")

    def test_tags_limit(self):
        desc = " ".join(f"#t{i}" for i in range(20))
        self.assertEqual(len(ds._tags_from_desc(desc).split(",")), 12)


class NormalizeTest(unittest.TestCase):
    def test_video_prefers_h264_and_cover(self):
        aweme = {
            "aweme_id": 123,
            "desc": "标题 #话题",
            "create_time": 1700000000,
            "images": [],
            "video": {
                "duration": 15267,
                "play_addr_h264": {"url_list": ["h264"]},
                "play_addr": {"url_list": ["play", "h264"]},
                "bit_rate": [{"bit_rate": 1000, "play_addr": {"url_list": ["low"]}}],
                "origin_cover": {"url_list": ["origin"]},
                "cover": {"url_list": ["cover"]},
            },
        }
        post = DouyinDownloader._normalize(aweme)
        self.assertEqual(post["aweme_id"], "123")
        self.assertFalse(post["is_image"])
        self.assertEqual(post["video_urls"], ["h264", "play", "low"])
        self.assertEqual(post["cover_url"], "origin")
        self.assertEqual(post["duration_s"], 15.3)
        self.assertEqual(post["url"], "https://www.douyin.com/video/123")

    def test_image_post_has_no_video_urls(self):
        aweme = {"aweme_id": 9, "images": [{"url_list": ["x"]}],
                 "video": {"duration": 0}}
        post = DouyinDownloader._normalize(aweme)
        self.assertTrue(post["is_image"])
        self.assertEqual(post["video_urls"], [])

    def test_extract_sec_uid(self):
        uid = "MS4wLjABAAAAxxx"
        self.assertEqual(DouyinDownloader.extract_sec_uid(
            f"https://www.douyin.com/user/{uid}?from_tab_name=main"), uid)
        self.assertEqual(DouyinDownloader.extract_sec_uid(uid), uid)
        with self.assertRaises(ValueError):
            DouyinDownloader.extract_sec_uid("")


class _FakeUploader:
    biliup_path = "biliup"

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def upload(self, video_file, **kwargs):
        self.calls.append((video_file, kwargs))
        if self.fail:
            raise RuntimeError("biliup failed")
        return {"code": 0, "data": {"bvid": "BV1xx", "aid": 42}}


class _FakeDownloader:
    def download(self, post, out):
        with open(out, "wb") as f:
            f.write(b"video")

    def download_cover(self, url, out):
        with open(out, "wb") as f:
            f.write(b"cover")
        return True

    def cleanup(self, *paths):
        for p in paths:
            if os.path.isfile(p):
                os.remove(p)


class UploadOneTest(unittest.TestCase):
    def setUp(self):
        self.old_cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        self.post = {"aweme_id": "111", "desc": "标题 #话题", "create_time": 1700000000,
                     "video_urls": ["u"], "cover_url": "http://c", "is_image": False}
        self.cfg = {"tags": "固定", "tid": 21, "copyright": 2, "desc": "简介"}

    def tearDown(self):
        os.chdir(self.old_cwd)

    def test_success_records_bvid_and_cleans_temp(self):
        seen = {}
        uploader = _FakeUploader()
        ds._upload_one(_FakeDownloader(), uploader, self.cfg, self.post, seen)
        self.assertEqual(seen["111"]["bvid"], "BV1xx")
        self.assertEqual(uploader.calls[0][1]["title"], "标题")
        self.assertEqual(uploader.calls[0][1]["tags"], "话题,固定")
        self.assertEqual(uploader.calls[0][1]["source"], "https://www.douyin.com/video/111")
        self.assertFalse(os.path.exists("111.mp4"))
        self.assertFalse(os.path.exists("111.jpg"))

    def test_failure_keeps_state_empty_and_cleans_temp(self):
        seen = {}
        with self.assertRaises(RuntimeError):
            ds._upload_one(_FakeDownloader(), _FakeUploader(fail=True),
                           self.cfg, self.post, seen)
        self.assertEqual(seen, {})
        self.assertFalse(os.path.exists("111.mp4"))
        self.assertFalse(os.path.exists("111.jpg"))


class StateFilesTest(unittest.TestCase):
    def test_load_channels_from_files_and_local(self):
        files = {ds.CHANNELS_FILE: json.dumps({"channels": [{"name": "gist"}]})}
        self.assertEqual(ds._load_channels(files, "missing.json")[0]["name"], "gist")
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf8") as f:
            json.dump([{"name": "local"}], f)
            path = f.name
        try:
            self.assertEqual(ds._load_channels({}, path)[0]["name"], "local")
        finally:
            os.remove(path)

    def test_save_state_dry_run_does_not_write(self):
        path = os.path.join(tempfile.mkdtemp(), "state.json")
        ds._save_state(None, {"a": 1}, path, dry_run=True)
        self.assertFalse(os.path.exists(path))
        ds._save_state(None, {"a": 1}, path)
        with open(path, encoding="utf8") as f:
            self.assertEqual(json.load(f), {"a": 1})


if __name__ == "__main__":
    unittest.main()
