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
        with mock.patch("bili_uploader.requests.post", side_effect=[resp1, resp2]) as m:
            self.u.upload_cover("12345", self.cover, title="t", tid="21", tags="a,b",
                                source="src", desc="d", copyright="2")
        # 图床：web/cover/up + base64 data URI + csrf
        c0 = m.call_args_list[0]
        self.assertEqual(c0.args[0], "https://member.bilibili.com/x/vu/web/cover/up")
        self.assertEqual(c0.kwargs["data"]["cover"],
                         "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8fakejpg").decode())
        self.assertEqual(c0.kwargs["data"]["csrf"], "csrf-token")
        self.assertNotIn("files", c0.kwargs)  # 不是 multipart
        # 编辑：web/edit?t=&csrf= + JSON body
        c1 = m.call_args_list[1]
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

    def test_dtime_passed_as_int(self):
        resp1, resp2 = mock.Mock(), mock.Mock()
        resp1.json.return_value = {"code": 0, "data": {"url": "http://x.jpg"}}
        resp2.json.return_value = {"code": 0, "data": {}}
        with mock.patch("bili_uploader.requests.post", side_effect=[resp1, resp2]) as m:
            self.u.upload_cover("1", self.cover, title="t", tid="21", tags="x",
                                dtime="2030-01-01 00:00:00")
        body = m.call_args_list[1].kwargs["json"]
        self.assertIsInstance(body["dtime"], int)
        self.assertGreater(body["dtime"], 1000000000)

    def test_non_json_still_diagnosable(self):
        resp = mock.Mock()
        resp.json.side_effect = ValueError("no json")
        resp.text = "html page"
        resp.status_code = 200
        with mock.patch("bili_uploader.requests.post", return_value=resp):
            with self.assertRaises(Exception) as ctx:
                self.u.upload_cover("1", self.cover)
        self.assertIn("html page", str(ctx.exception))

    def test_upload_cover_fail_not_block(self):
        raw = '{ code: 0, data: Some(Object { "aid": Number(999) }), message: "0" }'
        with mock.patch("bili_uploader.requests.post", side_effect=Exception("boom")):
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
