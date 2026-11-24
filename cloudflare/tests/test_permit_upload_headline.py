import importlib.util
from pathlib import Path
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('permit_page_build',ROOT/'build_permit_upload_page.py')
build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)

class Tests(unittest.TestCase):
    def setUp(self):self.page=(ROOT/'permit-upload-page.html').read_text()
    def test_saved_template_and_artifact_keep_approved_headline(self):
        build.validate(self.page)
        self.assertEqual((ROOT/'permit-upload-page.constant.js').read_text(),build.constant(self.page))
    def test_retired_visible_headline_or_title_is_rejected(self):
        for old in [self.page.replace('<h1>'+build.HEADLINE,'<h1>Newsroom report uploads'),self.page.replace('<title>'+build.TITLE,'<title>Newsroom report uploads | La Crosse Tribune')]:
            with self.assertRaises(ValueError):build.validate(old)
    def test_candidate_worker_must_embed_the_maintained_page(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'worker.js'
            p.write_text(build.constant(self.page)+'\nexport default {};\n')
            build.check_worker(p,self.page)
            p.write_text(p.read_text().replace(build.HEADLINE,'Newsroom report uploads'))
            with self.assertRaises(ValueError):build.check_worker(p,self.page)
            p.write_text('export default {};')
            with self.assertRaises(ValueError):build.check_worker(p,self.page)

if __name__=='__main__':unittest.main()
