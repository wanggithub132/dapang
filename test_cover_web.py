import json, tempfile, os, unittest, base64
from unittest import mock
import bili_uploader as bu

COOKIE = {"cookie_info": {"cookies": [{"name": "DedeUserID", "value": "123"},
                                       {"name": "bili_jct", "value": "csrf-token"}]}}

class TestCoverWeb(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cookie_file = os.path.join(self.dir, "cookies.json")
        with open(self.cookie_file, "w", encoding="utf8") as f:
            json.dump(COOKIE, f)
        self.cover = os.path.join(self.dir, "c.jpg")
        with open(self.cover, "wb") as f:
            f.write(b"\xff\xd8fakejpg")
        self.u = bu.BilibiliUploader(user_cookie=self.cookie_file, log=print)

    def test_web_cover_up_base64_and_edit_json(self):
        resp1, resp2 = mock.Mock(), mock.Mock()
        resp1.json.return_value = {"code": 0, "data": {"url": "http://i0.hdslb.com/bfs/archive/x.jpg"}}
        resp2.json.return_value = {"code": 0, "message": "0", "data": {}}
        with mock.patch("bili_uploader._cffi_requests") as m:
            m.post.side_effect = [resp1, resp2]
            self.u.upload_cover("12345", self.cover, title="t", tid="21", tags="a,b",
                                source="src", desc="d", copyright="2")
        # 图床：web/cover/up + base64 data URI + csrf + Chrome 指纹
        c0 = m.post.call_args_list[0]
        self.assertEqual(c0.args[0], "https://member.bilibili.com/x/vu/web/cover/up")
        self.assertEqual(c0.kwargs["impersonate"], "chrome")
        self.assertEqual(c0.kwargs["data"]["cover"],
                         "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8fakejpg").decode())
        self.assertEqual(c0.kwargs["data"]["csrf"], "csrf-token")
        self.assertNotIn("files", c0.kwargs)  # 不是 multipart
        # 编辑：web/edit?t=&csrf= + JSON body
        c1 = m.post.call_args_list[1]
        self.assertTrue(c1.args[0].startswith("https://member.bilibili.com/x/vu/web/edit?t="))
        self.assertIn("csrf=csrf-token", c1.args[0])
        body = c1.kwargs["json"]
        self.assertEqual(body["aid"], 12345)
        self.assertEqual(body["tid"], 21)
        self.assertEqual(body["copyright"], 2)
        self.assertTrue(body["cover"].startswith("https://"))
        self.assertEqual(body["tag"], "a,b")
        self.assertEqual(body["desc"], "d")
        self.assertEqual(body["desc_format_id"], 0)

    def test_edit_injects_videos_from_archive_view(self):
        # cookie 带 token_info 时，edit 前先调 client/view 拉真实 videos 注入
        with open(self.cookie_file, "w", encoding="utf8") as f:
            json.dump({"cookie_info": {"cookies": [{"name": "bili_jct", "value": "csrf-token"}]},
                       "token_info": {"access_token": "tok-abc"}}, f)
        resp1, resp2 = mock.Mock(), mock.Mock()
        resp1.json.return_value = {"code": 0, "data": {"url": "http://x.jpg"}}
        resp2.json.return_value = {"code": 0, "data": {}}
        view = mock.Mock()
        view.json.return_value = {"code": 0, "data": {
            "archive": {"cover": "http://c.jpg"},
            "videos": [{"aid": 1, "filename": "n2608random", "cid": 123}]}}
        with mock.patch("bili_uploader._cffi_requests") as m:
            m.get.return_value = view
            m.post.side_effect = [resp1, resp2]
            self.u.upload_cover("12345", self.cover, title="t", tid="21", tags="a,b")
        g0 = [c for c in m.get.call_args_list
              if "client/archive/view" in c.args[0]][0]
        self.assertTrue(g0.args[0].startswith(
            "https://member.bilibili.com/x/client/archive/view?access_key=tok-abc&aid=12345"))
        self.assertEqual(g0.kwargs["impersonate"], "chrome")
        body = m.post.call_args_list[1].kwargs["json"]
        self.assertEqual(body["videos"][0]["filename"], "n2608random")

    def test_dtime_passed_as_int(self):
        resp1, resp2 = mock.Mock(), mock.Mock()
        resp1.json.return_value = {"code": 0, "data": {"url": "http://x.jpg"}}
        resp2.json.return_value = {"code": 0, "data": {}}
        with mock.patch("bili_uploader._cffi_requests") as m:
            m.post.side_effect = [resp1, resp2]
            self.u.upload_cover("1", self.cover, title="t", tid="21", tags="x",
                                dtime="2030-01-01 00:00:00")
        body = m.post.call_args_list[1].kwargs["json"]
        self.assertIsInstance(body["dtime"], int)
        self.assertGreater(body["dtime"], 1000000000)

    def test_non_json_still_diagnosable(self):
        resp = mock.Mock()
        resp.json.side_effect = ValueError("no json")
        resp.text = "html page"
        resp.status_code = 200
        with mock.patch("bili_uploader._cffi_requests") as m:
            m.post.return_value = resp
            with self.assertRaises(Exception) as ctx:
                self.u.upload_cover("1", self.cover)
        self.assertIn("html page", str(ctx.exception))

    def test_requests_fallback_without_cffi(self):
        resp1 = mock.Mock()
        resp1.json.return_value = {"code": 0, "data": {"url": "http://i0.hdslb.com/bfs/archive/x.jpg"}}
        resp2 = mock.Mock()
        resp2.json.return_value = {"code": 0, "data": {}}
        with mock.patch("bili_uploader._cffi_requests", None), \
             mock.patch("bili_uploader.requests.get") as mg, \
             mock.patch("bili_uploader.requests.post", side_effect=[resp1, resp2]) as mp:
            mg.return_value.json.return_value = {"code": 0, "data": {"b_3": "x", "b_4": "y"}}
            self.u.upload_cover("1", self.cover, title="t", tid="21", tags="x")
        self.assertEqual(mp.call_args_list[0].args[0],
                         "https://member.bilibili.com/x/vu/web/cover/up")
        self.assertNotIn("impersonate", mp.call_args_list[0].kwargs)

    def test_upload_cover_fail_not_block(self):
        raw = '{ code: 0, data: Some(Object { "aid": Number(999) }), message: "0" }'
        with mock.patch("bili_uploader._cffi_requests") as m:
            m.post.side_effect = Exception("boom")
            with mock.patch("subprocess.Popen") as popen:
                proc = mock.Mock()
                proc.returncode = 0
                proc.communicate.return_value = (raw.encode() + b"\nprogress\n", b"")
                popen.return_value = proc
                self.u.biliup_path = "biliup"
                ret = self.u.upload("a.mp4", title="t", tid="21", tags="x", cover=self.cover)
        self.assertEqual(ret["code"], 0)

if __name__ == "__main__":
    unittest.main(verbosity=2)
