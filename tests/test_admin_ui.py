import unittest
from pathlib import Path


ADMIN_HTML = Path(__file__).resolve().parents[1] / "static" / "admin.html"


class AdminUiTests(unittest.TestCase):
    def setUp(self):
        self.html = ADMIN_HTML.read_text(encoding="utf-8")

    def test_admin_brand_does_not_link_to_supplier_form(self):
        self.assertNotIn('href="/" class="brand"', self.html)
        self.assertIn('href="/">前台提交页</a>', self.html)

    def test_manual_task_buttons_open_admin_modal(self):
        self.assertIn('data-open-manual-task', self.html)
        self.assertIn('id="manualTaskModal"', self.html)
        self.assertIn('管理员新建压测任务', self.html)

    def test_manual_task_form_has_admin_concurrency_controls(self):
        self.assertIn('压测并发次数', self.html)
        self.assertIn('是否阶梯压测', self.html)
        self.assertIn('id="manualConcurrency"', self.html)
        self.assertIn('id="manualStepped"', self.html)

    def test_sample_image_uses_in_page_preview_not_blank_target(self):
        self.assertIn('id="imagePreviewModal"', self.html)
        self.assertIn('data-preview-image', self.html)
        self.assertNotIn('target="_blank" rel="noreferrer">查看样图</a>', self.html)

    def test_task_cards_render_request_details(self):
        self.assertIn('parseRequestDetails', self.html)
        self.assertIn('renderRequestDetails', self.html)
        self.assertIn('接口请求详情', self.html)


if __name__ == "__main__":
    unittest.main()
