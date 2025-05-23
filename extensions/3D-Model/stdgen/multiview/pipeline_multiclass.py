import inspect
import warnings
from typing import Callable, List, Optional, Union, Dict, Any
import PIL
import torch
import kornia
from transformers import CLIPVisionModelWithProjection, CLIPFeatureExtractor, CLIPTextModel
from diffusers.utils.import_utils import is_accelerate_available
from diffusers.image_processor import VaeImageProcessor
from diffusers.models import AutoencoderKL, UNet2DConditionModel
from diffusers.models.embeddings import get_timestep_embedding
from diffusers.schedulers import KarrasDiffusionSchedulers
from diffusers.utils import deprecate, logging
from diffusers.utils.torch_utils import randn_tensor
from diffusers.pipelines.pipeline_utils import DiffusionPipeline, ImagePipelineOutput
from diffusers.pipelines.stable_diffusion.stable_unclip_image_normalizer import StableUnCLIPImageNormalizer
import torchvision.transforms.functional as TF
from einops import rearrange
logger = logging.get_logger(__name__)


def CLIP_preprocess(x):
    dtype = x.dtype
    # following openai's implementation
    # TODO HF OpenAI CLIP preprocessing issue https://github.com/huggingface/transformers/issues/22505#issuecomment-1650170741
    # follow openai preprocessing to keep exact same, input tensor [-1, 1], otherwise the preprocessing will be different, https://github.com/huggingface/transformers/pull/22608
    if isinstance(x, torch.Tensor):
        if x.min() < -1.0 or x.max() > 1.0:
            raise ValueError("Expected input tensor to have values in the range [-1, 1]")
    x = kornia.geometry.resize(x.to(torch.float32), (224, 224), interpolation='bicubic', align_corners=True, antialias=False).to(dtype=dtype)
    x = (x + 1.) / 2.
    # renormalize according to clip
    x = kornia.enhance.normalize(x, torch.Tensor([0.48145466, 0.4578275, 0.40821073]),
                                 torch.Tensor([0.26862954, 0.26130258, 0.27577711]))
    return x


class StableUnCLIPImg2ImgPipeline(DiffusionPipeline):
    """
    Pipeline for text-guided image to image generation using stable unCLIP.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Args:
        feature_extractor ([`CLIPFeatureExtractor`]):
            Feature extractor for image pre-processing before being encoded.
        image_encoder ([`CLIPVisionModelWithProjection`]):
            CLIP vision model for encoding images.
        image_normalizer ([`StableUnCLIPImageNormalizer`]):
            Used to normalize the predicted image embeddings before the noise is applied and un-normalize the image
            embeddings after the noise has been applied.
        image_noising_scheduler ([`KarrasDiffusionSchedulers`]):
            Noise schedule for adding noise to the predicted image embeddings. The amount of noise to add is determined
            by `noise_level` in `StableUnCLIPPipeline.__call__`.
        text_encoder ([`CLIPTextModel`]):
            Frozen text-encoder.
        unet ([`UNet2DConditionModel`]): Conditional U-Net architecture to denoise the encoded image latents.
        scheduler ([`KarrasDiffusionSchedulers`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents.
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) Model to encode and decode images to and from latent representations.
    """
    # image encoding components
    feature_extractor: CLIPFeatureExtractor
    image_encoder: CLIPVisionModelWithProjection
    # image noising components
    image_normalizer: StableUnCLIPImageNormalizer
    image_noising_scheduler: KarrasDiffusionSchedulers
    # regular denoising components
    text_encoder: CLIPTextModel
    unet: UNet2DConditionModel
    scheduler: KarrasDiffusionSchedulers
    vae: AutoencoderKL

    def __init__(
        self,
        # image encoding components
        feature_extractor: CLIPFeatureExtractor,
        image_encoder: CLIPVisionModelWithProjection,
        # image noising components
        image_normalizer: StableUnCLIPImageNormalizer,
        image_noising_scheduler: KarrasDiffusionSchedulers,
        # regular denoising components
        text_encoder: CLIPTextModel,
        unet: UNet2DConditionModel,
        scheduler: KarrasDiffusionSchedulers,
        # vae
        vae: AutoencoderKL,
        num_views: int = 4,
    ):
        super().__init__()

        self.register_modules(
            feature_extractor=feature_extractor,
            image_encoder=image_encoder,
            image_normalizer=image_normalizer,
            image_noising_scheduler=image_noising_scheduler,
            text_encoder=text_encoder,
            unet=unet,
            scheduler=scheduler,
            vae=vae,
        )
        self.vae_scale_factor = 2 ** (len(self.vae.config.block_out_channels) - 1)
        self.image_processor = VaeImageProcessor(vae_scale_factor=self.vae_scale_factor)
        self.num_views: int = num_views
    # Copied from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion.StableDiffusionPipeline.enable_vae_slicing
    def enable_vae_slicing(self):
        r"""
        Enable sliced VAE decoding.

        When this option is enabled, the VAE will split the input tensor in slices to compute decoding in several
        steps. This is useful to save some memory and allow larger batch sizes.
        """
        self.vae.enable_slicing()

    # Copied from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion.StableDiffusionPipeline.disable_vae_slicing
    def disable_vae_slicing(self):
        r"""
        Disable sliced VAE decoding. If `enable_vae_slicing` was previously invoked, this method will go back to
        computing decoding in one step.
        """
        self.vae.disable_slicing()

    def enable_sequential_cpu_offload(self, gpu_id=0):
        r"""
        Offloads all models to CPU using accelerate, significantly reducing memory usage. When called, the pipeline's
        models have their state dicts saved to CPU and then are moved to a `torch.device('meta') and loaded to GPU only
        when their specific submodule has its `forward` method called.
        """
        if is_accelerate_available():
            from accelerate import cpu_offload
        else:
            raise ImportError("Please install accelerate via `pip install accelerate`")

        device = torch.device(f"cuda:{gpu_id}")

        # TODO: self.image_normalizer.{scale,unscale} are not covered by the offload hooks, so they fails if added to the list
        models = [
            self.image_encoder,
            self.text_encoder,
            self.unet,
            self.vae,
        ]
        for cpu_offloaded_model in models:
            if cpu_offloaded_model is not None:
                cpu_offload(cpu_offloaded_model, device)

    @property
    # Copied from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion.StableDiffusionPipeline._execution_device
    def _execution_device(self):
        r"""
        Returns the device on which the pipeline's models will be executed. After calling
        `pipeline.enable_sequential_cpu_offload()` the execution device can only be inferred from Accelerate's module
        hooks.
        """
        if not hasattr(self.unet, "_hf_hook"):
            return self.device
        for module in self.unet.modules():
            if (
                hasattr(module, "_hf_hook")
                and hasattr(module._hf_hook, "execution_device")
                and module._hf_hook.execution_device is not None
            ):
                return torch.device(module._hf_hook.execution_device)
        return self.device

    # Copied from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion.StableDiffusionPipeline._encode_prompt
    def _encode_prompt(
        self,
        prompt,
        device,
        num_images_per_prompt,
        do_classifier_free_guidance,
        negative_prompt=None,
        prompt_embeds: Optional[torch.FloatTensor] = None,
        negative_prompt_embeds: Optional[torch.FloatTensor] = None,
        lora_scale: Optional[float] = None,
    ):
        r"""
        Encodes the prompt into text encoder hidden states.

        Args:
             prompt (`str` or `List[str]`, *optional*):
                prompt to be encoded
            device: (`torch.device`):
                torch device
            num_images_per_prompt (`int`):
                number of images that should be generated per prompt
            do_classifier_free_guidance (`bool`):
                whether to use classifier free guidance or not
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. If not defined, one has to pass
                `negative_prompt_embeds`. instead. If not defined, one has to pass `negative_prompt_embeds`. instead.
                Ignored when not using guidance (i.e., ignored if `guidance_scale` is less than `1`).
            prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
                provided, text embeddings will be generated from `prompt` input argument.
            negative_prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
                weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
                argument.
        """
        prompt_embeds = prompt_embeds.to(dtype=self.text_encoder.dtype, device=device)

        if do_classifier_free_guidance:
            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            normal_prompt_embeds, color_prompt_embeds = torch.chunk(prompt_embeds, 2, dim=0)
            
            prompt_embeds = torch.cat([normal_prompt_embeds, normal_prompt_embeds, color_prompt_embeds, color_prompt_embeds], 0)

        return prompt_embeds

    def _encode_image(
        self,
        # image_pil,
        image,
        device,
        num_images_per_prompt,
        do_classifier_free_guidance,
        noise_level: int=0,
        class_targets: list=None,
        generator: Optional[torch.Generator] = None
    ):
        dtype = next(self.image_encoder.parameters()).dtype
        # ______________________________clip image embedding______________________________ 
        image_ = CLIP_preprocess(image)
        image_embeds = self.image_encoder(image_).image_embeds
        
        image_embeds_ls = []

        for class_target in class_targets:
            image_embeds_ls.append(self.noise_image_embeddings(
                image_embeds=image_embeds,
                noise_level=noise_level,
                class_target=class_target,
                generator=generator,
                ).repeat(num_images_per_prompt, 1))

        if do_classifier_free_guidance:
            for idx in range(len(image_embeds_ls)):
                normal_image_embeds, color_image_embeds = torch.chunk(image_embeds_ls[idx], 2, dim=0)
                negative_prompt_embeds = torch.zeros_like(normal_image_embeds)

                # For classifier free guidance, we need to do two forward passes.
                # Here we concatenate the unconditional and text embeddings into a single batch
                # to avoid doing two forward passes
                image_embeds_ls[idx] = torch.cat([negative_prompt_embeds, normal_image_embeds, negative_prompt_embeds, color_image_embeds], 0)
            
        # _____________________________vae input latents__________________________________________________
        image_latents = self.vae.encode(image.to(self.vae.dtype)).latent_dist.mode() * self.vae.config.scaling_factor
        # Note: repeat differently from official pipelines     
        image_latents = image_latents.repeat(num_images_per_prompt, 1, 1, 1)

        if do_classifier_free_guidance:
            normal_image_latents, color_image_latents = torch.chunk(image_latents, 2, dim=0)
            image_latents = torch.cat([torch.zeros_like(normal_image_latents), normal_image_latents, 
                                       torch.zeros_like(color_image_latents), color_image_latents], 0)

        return image_embeds_ls, image_latents

    # Copied from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion.StableDiffusionPipeline.decode_latents
    def decode_latents(self, latents):
        latents = 1 / self.vae.config.scaling_factor * latents
        image = self.vae.decode(latents).sample
        image = (image / 2 + 0.5).clamp(0, 1)
        # we always cast to float32 as this does not cause significant overhead and is compatible with bfloat16
        image = image.cpu().permute(0, 2, 3, 1).float().numpy()
        return image

    # Copied from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion.StableDiffusionPipeline.prepare_extra_step_kwargs
    def prepare_extra_step_kwargs(self, generator, eta):
        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]

        accepts_eta = "eta" in set(inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        # check if the scheduler accepts generator
        accepts_generator = "generator" in set(inspect.signature(self.scheduler.step).parameters.keys())
        if accepts_generator:
            extra_step_kwargs["generator"] = generator
        return extra_step_kwargs

    def check_inputs(
        self,
        prompt,
        image,
        height,
        width,
        callback_steps,
        noise_level,
    ):
        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(f"`height` and `width` have to be divisible by 8 but are {height} and {width}.")

        if (callback_steps is None) or (
            callback_steps is not None and (not isinstance(callback_steps, int) or callback_steps <= 0)
        ):
            raise ValueError(
                f"`callback_steps` has to be a positive integer but is {callback_steps} of type"
                f" {type(callback_steps)}."
            )

        if prompt is not None and (not isinstance(prompt, str) and not isinstance(prompt, list)):
            raise ValueError(f"`prompt` has to be of type `str` or `list` but is {type(prompt)}")


        if noise_level < 0 or noise_level >= self.image_noising_scheduler.config.num_train_timesteps:
            raise ValueError(
                f"`noise_level` must be between 0 and {self.image_noising_scheduler.config.num_train_timesteps - 1}, inclusive."
            )

    # Copied from diffusers.pipelines.stable_diffusion.pipeline_stable_diffusion.StableDiffusionPipeline.prepare_latents
    def prepare_latents(self, batch_size, num_channels_latents, height, width, dtype, device, generator, latents=None):
        shape = (batch_size, num_channels_latents, height // self.vae_scale_factor, width // self.vae_scale_factor)
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f"You have passed a list of generators of length {len(generator)}, but requested an effective batch"
                f" size of {batch_size}. Make sure the batch size matches the length of the generators."
            )

        if latents is None:
            noise = randn_tensor(shape, generator=generator, device=device, dtype=dtype)
            latents = noise.clone()
        else:
            latents = latents.to(device)

        # scale the initial noise by the standard deviation required by the scheduler
        latents = latents * self.scheduler.init_noise_sigma
        return latents, noise

    # Copied from diffusers.pipelines.stable_diffusion.pipeline_stable_unclip.StableUnCLIPPipeline.noise_image_embeddings
    def noise_image_embeddings(
        self,
        image_embeds: torch.Tensor,
        noise_level: int,
        class_target: torch.Tensor,
        noise: Optional[torch.FloatTensor] = None,
        generator: Optional[torch.Generator] = None,
    ):
        """
        Add noise to the image embeddings. The amount of noise is controlled by a `noise_level` input. A higher
        `noise_level` increases the variance in the final un-noised images.

        The noise is applied in two ways
        1. A noise schedule is applied directly to the embeddings
        2. A vector of sinusoidal time embeddings are appended to the output.

        In both cases, the amount of noise is controlled by the same `noise_level`.

        The embeddings are normalized before the noise is applied and un-normalized after the noise is applied.
        """
        if noise is None:
            noise = randn_tensor(
                image_embeds.shape, generator=generator, device=image_embeds.device, dtype=image_embeds.dtype
            )

        noise_level = torch.tensor([noise_level] * image_embeds.shape[0], device=image_embeds.device)

        dtype = image_embeds.dtype

        image_embeds = self.image_normalizer.scale(image_embeds)

        image_embeds = self.image_noising_scheduler.add_noise(image_embeds, timesteps=noise_level, noise=noise)

        image_embeds = self.image_normalizer.unscale(image_embeds)

        noise_level = get_timestep_embedding(
            timesteps=noise_level, embedding_dim=image_embeds.shape[-1], flip_sin_to_cos=True, downscale_freq_shift=0
        )

        # `get_timestep_embeddings` does not contain any weights and will always return f32 tensors,
        # but we might actually be running in fp16. so we need to cast here.
        # there might be better ways to encapsulate this.
        image_embeds = image_embeds.to(dtype=dtype)
        noise_level = noise_level.to(image_embeds.dtype)

        image_embeds = torch.cat((image_embeds, class_target.repeat(image_embeds.shape[0] // class_target.shape[0], 1)), 1)

        return image_embeds


    @torch.no_grad()
    def __call__(
        self,
        image: Union[torch.FloatTensor, PIL.Image.Image],
        prompt: Union[str, List[str]],   
        prompt_embeds: torch.FloatTensor = None,
        dino_feature: torch.FloatTensor = None,
        height: Optional[int] = None,
        width: Optional[int] = None,
        num_inference_steps: int = 20,
        guidance_scale: float = 10,
        negative_prompt: Optional[Union[str, List[str]]] = None,
        num_images_per_prompt: Optional[int] = 1,
        eta: float = 0.0,
        generator: Optional[torch.Generator] = None,
        latents: Optional[torch.FloatTensor] = None,
        negative_prompt_embeds: Optional[torch.FloatTensor] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
        callback_steps: int = 1,
        cross_attention_kwargs: Optional[Dict[str, Any]] = None,
        noise_level: int = 0,
        image_embeds: Optional[torch.FloatTensor] = None,
        return_elevation_focal: Optional[bool] = False,
        gt_img_in: Optional[torch.FloatTensor] = None,
        num_levels: Optional[int] = 3,
    ):
        r"""
        Function invoked when calling the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide the image generation. If not defined, one has to pass `prompt_embeds`.
                instead.
            image (`torch.FloatTensor` or `PIL.Image.Image`):
                `Image`, or tensor representing an image batch. The image will be encoded to its CLIP embedding which
                the unet will be conditioned on. Note that the image is _not_ encoded by the vae and then used as the
                latents in the denoising process such as in the standard stable diffusion text guided image variation
                process.
            height (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 20):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 10.0):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. If not defined, one has to pass
                `negative_prompt_embeds`. instead. If not defined, one has to pass `negative_prompt_embeds`. instead.
                Ignored when not using guidance (i.e., ignored if `guidance_scale` is less than `1`).
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to
                [`schedulers.DDIMScheduler`], will be ignored for others.
            generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
                One or a list of [torch generator(s)](https://pytorch.org/docs/stable/generated/torch.Generator.html)
                to make generation deterministic.
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor will ge generated by sampling using the supplied random `generator`.
            prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
                provided, text embeddings will be generated from `prompt` input argument.
            negative_prompt_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
                weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
                argument.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between
                [PIL](https://pillow.readthedocs.io/en/stable/): `PIL.Image.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.
            callback (`Callable`, *optional*):
                A function that will be called every `callback_steps` steps during inference. The function will be
                called with the following arguments: `callback(step: int, timestep: int, latents: torch.FloatTensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function will be called. If not specified, the callback will be
                called at every step.
            cross_attention_kwargs (`dict`, *optional*):
                A kwargs dictionary that if specified is passed along to the `AttnProcessor` as defined under
                `self.processor` in
                [diffusers.cross_attention](https://github.com/huggingface/diffusers/blob/main/src/diffusers/models/cross_attention.py).
            noise_level (`int`, *optional*, defaults to `0`):
                The amount of noise to add to the image embeddings. A higher `noise_level` increases the variance in
                the final un-noised images. See `StableUnCLIPPipeline.noise_image_embeddings` for details.
            image_embeds (`torch.FloatTensor`, *optional*):
                Pre-generated CLIP embeddings to condition the unet on. Note that these are not latents to be used in
                the denoising process. If you want to provide pre-generated latents, pass them to `__call__` as
                `latents`.

        Examples:

        Returns:
            [`~pipelines.ImagePipelineOutput`] or `tuple`: [`~ pipeline_utils.ImagePipelineOutput`] if `return_dict` is
            True, otherwise a `tuple`. When returning a tuple, the first element is a list with the generated images.
        """
        # 0. Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        # 1. Check inputs. Raise error if not correct
        self.check_inputs(
            prompt=prompt,
            image=image,
            height=height,
            width=width,
            callback_steps=callback_steps,
            noise_level=noise_level
        )

        # 2. Define call parameters
        if isinstance(image, list):
            batch_size = len(image)
        elif isinstance(image, torch.Tensor):
            batch_size = image.shape[0]
            assert batch_size >= self.num_views and batch_size % self.num_views == 0
        elif isinstance(image, PIL.Image.Image):
            image = [image]*self.num_views*2
            batch_size = self.num_views*2

        if isinstance(prompt, str):
            prompt = [prompt] * self.num_views * 2

        device = self._execution_device

        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale != 1.0

        # 3. Encode input prompt
        text_encoder_lora_scale = (
            cross_attention_kwargs.get("scale", None) if cross_attention_kwargs is not None else None
        )
        prompt_embeds = self._encode_prompt(
            prompt=prompt,
            device=device,
            num_images_per_prompt=num_images_per_prompt,
            do_classifier_free_guidance=do_classifier_free_guidance,
            negative_prompt=negative_prompt,
            prompt_embeds=prompt_embeds,
            negative_prompt_embeds=negative_prompt_embeds,
            lora_scale=text_encoder_lora_scale,
        )
        

        # 4. Encoder input image
        noise_level = torch.tensor([noise_level], device=device)

        class_targets = []
        for level in [0, 1, 2]:
            class_target = torch.tensor([0, 0, 0, 0]).cuda()
            class_target[level] = 1
            class_target = torch.repeat_interleave(class_target, 256).unsqueeze(0)
            class_targets.append(class_target)

        image_embeds_ls, image_latents = self._encode_image(
            image=image,
            device=device,
            num_images_per_prompt=num_images_per_prompt,
            do_classifier_free_guidance=do_classifier_free_guidance,
            noise_level=noise_level,
            class_targets=class_targets,
            generator=generator,
        )

        # 5. Prepare timesteps
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps

        # 6. Prepare latent variables
        num_channels_latents = self.unet.config.out_channels
        if gt_img_in is not None:
            latents = gt_img_in * self.scheduler.init_noise_sigma
        else:
            latents, noise = self.prepare_latents(
                batch_size=batch_size,
                num_channels_latents=num_channels_latents,
                height=height,
                width=width,
                dtype=prompt_embeds.dtype,
                device=device,
                generator=generator,
                latents=latents,
            )

        # 7. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)

        original_latents = latents.clone()
        image_ls = []
        for level in range(num_levels):
            latents = original_latents.clone()
            eles, focals = [], []
            # 8. Denoising loop
            for i, t in enumerate(self.progress_bar(timesteps)):
                if do_classifier_free_guidance:
                    normal_latents, color_latents = torch.chunk(latents, 2, dim=0)  
                    latent_model_input = torch.cat([normal_latents, normal_latents, color_latents, color_latents], 0)
                else:
                    latent_model_input = latents

                latent_model_input = torch.cat([
                        latent_model_input, image_latents
                    ], dim=1)
                latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)

                # predict the noise residual
                unet_out = self.unet(
                    latent_model_input,
                    t,
                    encoder_hidden_states=prompt_embeds,
                    dino_feature=dino_feature,
                    class_labels=image_embeds_ls[level],
                    cross_attention_kwargs=cross_attention_kwargs,
                    return_dict=False)
                
                noise_pred = unet_out[0]
                if return_elevation_focal:    
                    uncond_pose, pose  = torch.chunk(unet_out[1], 2, 0) 
                    pose = uncond_pose + guidance_scale * (pose - uncond_pose)
                    ele = pose[:, 0].detach().cpu().numpy() # b
                    eles.append(ele)
                    focal = pose[:, 1].detach().cpu().numpy()
                    focals.append(focal)
                    
                # perform guidance
                if do_classifier_free_guidance:
                    normal_noise_pred_uncond, normal_noise_pred_text, color_noise_pred_uncond, color_noise_pred_text = torch.chunk(noise_pred, 4, dim=0)
                    
                    noise_pred_uncond, noise_pred_text = torch.cat([normal_noise_pred_uncond, color_noise_pred_uncond], 0), torch.cat([normal_noise_pred_text, color_noise_pred_text], 0)
                    noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)
                    
                # compute the previous noisy sample x_t -> x_t-1
                latents = self.scheduler.step(noise_pred, t, latents, **extra_step_kwargs, return_dict=False)[0]

                if callback is not None and i % callback_steps == 0:
                    callback(i, t, latents)

            # 9. Post-processing
            if not output_type == "latent":
                if num_channels_latents == 8:
                    latents = torch.cat([latents[:, :4], latents[:, 4:]], dim=0)
                with torch.no_grad():
                    image = self.vae.decode((latents / self.vae.config.scaling_factor).to(self.vae.dtype), return_dict=False)[0]
            else:
                image = latents

            image = self.image_processor.postprocess(image, output_type=output_type)
            image = ImagePipelineOutput(images=image)
            image_ls.append(image)

        return image_ls
