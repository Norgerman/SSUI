import unittest
from tests.utils import should_run_slow_tests

@unittest.skipIf(not should_run_slow_tests(), "Skipping slow test")
class TestSD1(unittest.TestCase):
    def setUp(self):
        from ssui_image.SD1 import SD1Model
        from ssui.config import SSUIConfig
        from ssui.base import Prompt
        self.model = SD1Model.load("D:\\StableDiffusion\\stable-diffusion-webui\\models\\Stable-diffusion\\anything-v3-2700c435.ckpt")
        self.config = SSUIConfig()
        self.config.set_prepared(False)

        self.positive = Prompt("a beautiful girl, masterpiece, best quality")
        self.negative = Prompt("a bad image")

    
    def test_workflow(self):
        from ssui_image.SD1 import (
            SD1Clip, SD1Latent, 
            SD1Denoise, SD1LatentDecode
        )
        positive, negative = SD1Clip(self.config("Prompt To Condition"), self.model, self.positive, self.negative)
        latent = SD1Latent(self.config("Create Empty Latent"))
        latent = SD1Denoise(self.config("Denoise"), self.model, latent, positive, negative)
        image = SD1LatentDecode(self.config("Latent to Image"), self.model, latent)
        image._image.save("result2.png")
