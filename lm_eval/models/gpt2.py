import torch
import transformers
from typing import Optional, Union
from lm_eval.base import BaseLM


def _get_dtype(dtype: Union[str, torch.dtype]) -> torch.dtype:
    """Converts `dtype` from `str` to torch.dtype when possible. Does not use an instantiated HF AutoConfig"""
    if isinstance(dtype, str) and dtype != "auto":
        # Convert `str` args torch dtype: `float16` -> `torch.float16`
        _torch_dtype = getattr(torch, dtype)
    else:
        _torch_dtype = dtype
    return _torch_dtype


class HFLM(BaseLM):

    _DEFAULT_MAX_LENGTH = 2048

    def __init__(
        self,
        device="cuda",
        pretrained="gpt2",
        revision="main",
        low_cpu_mem_usage=None,
        subfolder=None,
        tokenizer=None,
        batch_size=1,
        max_batch_size=512,
        max_length=None,
        load_in_8bit: Optional[bool] = False,
        trust_remote_code: Optional[bool] = True,
        dtype: Optional[Union[str, torch.dtype]] = "auto",
    ):
        super().__init__()

        # pretrained: EleutherAI/pythia-160m, device: cuda:0, revision: main, subfolder: None, tokenizer: None, batch_size: 1, max_batch_size: 512, max_length: None, load_in_8bit: False, trust_remote_code: False, dtype: auto
        print(f'>>>pretrained: {pretrained}, device: {device}, revision: {revision}, subfolder: {subfolder}, tokenizer: {tokenizer}, batch_size: {batch_size}, max_batch_size: {max_batch_size}, max_length: {max_length}, load_in_8bit: {load_in_8bit}, trust_remote_code: {trust_remote_code}, dtype: {dtype}')

        # Initialize model
        if isinstance(pretrained, transformers.PreTrainedModel):
            self.model = pretrained
            self._device = self.model.device

            if tokenizer:
                assert isinstance(
                    tokenizer, transformers.PreTrainedTokenizer
                ) or isinstance(tokenizer, transformers.PreTrainedTokenizerFast)
                self.tokenizer = tokenizer
            else:
                # Get tokenizer
                model_name = self.model.name_or_path
                self.tokenizer = transformers.AutoTokenizer.from_pretrained(
                    model_name,
                    revision=revision,
                    trust_remote_code=trust_remote_code,
                )

        elif isinstance(pretrained, str):

            # Initialize device
            assert isinstance(device, str)
            device_list = set(
                ["cuda", "cpu"]
                + [f"cuda:{i}" for i in range(torch.cuda.device_count())]
            )
            if device and device in device_list:
                self._device = torch.device(device)
                print(f"Using device '{device}'")
            else:
                print("Device not specified")
                print(f"Cuda Available? {torch.cuda.is_available()}")
                self._device = (
                    torch.device("cuda")
                    if torch.cuda.is_available()
                    else torch.device("cpu")
                )
            revision = revision + ("/" + subfolder if subfolder is not None else "")

            # Initialize new model and tokenizer instances
            from modelscope.utils.hf_util import AutoModelForCausalLM       # TODO: ONLY FOR TEST !
            # self.model = transformers.AutoModelForCausalLM.from_pretrained(
            self.model = AutoModelForCausalLM.from_pretrained(
                pretrained,
                load_in_8bit=load_in_8bit,
                low_cpu_mem_usage=low_cpu_mem_usage,
                revision=revision,
                torch_dtype=_get_dtype(dtype),
                trust_remote_code=trust_remote_code,
            ).to(self.device)

            from modelscope.utils.hf_util import AutoTokenizer      # TODO: ONLY FOR TEST
            # self.tokenizer = transformers.AutoTokenizer.from_pretrained(
            self.tokenizer = AutoTokenizer.from_pretrained(
                tokenizer if tokenizer else pretrained,
                revision=revision,
                trust_remote_code=trust_remote_code,
            )

        else:
            raise TypeError(
                "Parameter pretrained should be of type str or transformers.PreTrainedModel"
            )

        # print(f'>>>model object in HFLM init: {self.model}')
        # GPTNeoXForCausalLM(
        #   (gpt_neox): GPTNeoXModel(
        #     (embed_in): Embedding(50304, 768)
        #     (emb_dropout): Dropout(p=0.0, inplace=False)
        #     (layers): ModuleList(
        #       (0-11): 12 x GPTNeoXLayer(
        #         (input_layernorm): LayerNorm((768,), eps=1e-05, elementwise_affine=True)
        #         (post_attention_layernorm): LayerNorm((768,), eps=1e-05, elementwise_affine=True)
        #         (post_attention_dropout): Dropout(p=0.0, inplace=False)
        #         (post_mlp_dropout): Dropout(p=0.0, inplace=False)
        #         (attention): GPTNeoXAttention(
        #           (rotary_emb): GPTNeoXRotaryEmbedding()
        #           (query_key_value): Linear(in_features=768, out_features=2304, bias=True)
        #           (dense): Linear(in_features=768, out_features=768, bias=True)
        #           (attention_dropout): Dropout(p=0.0, inplace=False)
        #         )
        #         (mlp): GPTNeoXMLP(
        #           (dense_h_to_4h): Linear(in_features=768, out_features=3072, bias=True)
        #           (dense_4h_to_h): Linear(in_features=3072, out_features=768, bias=True)
        #           (act): GELUActivation()
        #         )
        #       )
        #     )
        #     (final_layer_norm): LayerNorm((768,), eps=1e-05, elementwise_affine=True)
        #   )
        #   (embed_out): Linear(in_features=768, out_features=50304, bias=False)
        # )

        self.model.eval()

        self.vocab_size = self.tokenizer.vocab_size

        # Validate batch_size
        assert isinstance(batch_size, (int, str))

        # setup for automatic batch size detection
        if str(batch_size).startswith("auto"):
            batch_size = batch_size.split(":")
            self.batch_size_per_gpu = batch_size[0]
            self.batch_schedule = float(batch_size[1]) if len(batch_size) > 1 else 1
        else:
            self.batch_size_per_gpu = int(batch_size)
        self.max_batch_size = max_batch_size

        self._max_length = max_length

    @property
    def eot_token_id(self):
        # we use EOT because end of *text* is more accurate for what we're doing than end of *sentence*
        return self.tokenizer.eos_token_id

    @property
    def max_length(self):
        if self._max_length:  # if max length manually set, return it
            return self._max_length
        seqlen_config_attrs = ("n_positions", "max_position_embeddings", "n_ctx")
        for attr in seqlen_config_attrs:
            if hasattr(self.model.config, attr):
                return getattr(self.model.config, attr)
        if hasattr(self.tokenizer, "model_max_length"):
            if self.tokenizer.model_max_length == 1000000000000000019884624838656:
                return self._DEFAULT_MAX_LENGTH
            return self.tokenizer.model_max_length
        return self._DEFAULT_MAX_LENGTH

    @property
    def max_gen_toks(self):
        return 256

    @property
    def batch_size(self):
        # TODO: fix multi-gpu
        return self.batch_size_per_gpu  # * gpus

    @property
    def device(self):
        # TODO: fix multi-gpu
        return self._device

    def tok_encode(self, string: str):
        return self.tokenizer.encode(string, add_special_tokens=False)

    def tok_decode(self, tokens):
        return self.tokenizer.decode(tokens)

    def _model_call(self, inps):
        """
        inps: a torch tensor of shape [batch, sequence]
        the size of sequence may vary from call to call

        returns: a torch tensor of shape [batch, sequence, vocab] with the
        logits returned from the model
        """
        with torch.no_grad():
            # res = self.model(inps): CausalLMOutputWithPast; res[0] shape: torch.Size([1, 222, 50304])
            # for ceval: inps shape: torch.Size([1, 83]), shape: torch.Size([1, 83, 50304])
            # self.model: LlamaForCausalLM (for llama2)
            print(f'>>self.model: {self.model}')
            return self.model(inps)[0]

    def _model_generate(self, context, max_length, eos_token_id):
        print('\n>>> _model_generate: ')
        print(f'>>context:\n{context}, >shape:{context.shape}')     # tensor([[37857, 92345,  3144,  ...,     5, 58182, 92345]], device='cuda:0')
        print(f'>>max_length:\n{max_length}')       # 1898
        print(f'>>eos_token_id:\n{eos_token_id}')       # 92345
        if hasattr(self.model, 'generation_config'):
            print(f'>>generation_config:\n{self.model.generation_config}')
            # GenerationConfig {
            #   "assistant_token_id": 196,
            #   "bos_token_id": 1,
            #   "do_sample": true,      # --> false
            #   "eos_token_id": 2,
            #   "max_new_tokens": 2048,
            #   "pad_token_id": 0,
            #   "repetition_penalty": 1.05,
            #   "temperature": 0.3,
            #   "top_k": 5,
            #   "top_p": 0.85,
            #   "user_token_id": 195
            # }

        generation_kwargs = {"do_sample": False, "max_length": max_length}
        if eos_token_id is not None:
            generation_kwargs["eos_token_id"] = eos_token_id
            generation_kwargs[
                "pad_token_id"
            ] = eos_token_id  # setting eos_token_id as pad token

        print(f'>>true_generation_kwargs:\n{generation_kwargs}')

        return self.model.generate(context, **generation_kwargs)


# for backwards compatibility
GPT2LM = HFLM

class OPTIMUMLM(BaseLM):
    def __init__(
        self,
        device="cpu",
        pretrained="gpt2",
        revision="main",
        low_cpu_mem_usage=None,
        subfolder=None,
        tokenizer=None,
        batch_size=1,
        load_in_8bit: Optional[bool] = False,
        trust_remote_code: Optional[bool] = False,
    ):
        super().__init__()

        import optimum
        from optimum.intel.openvino import OVModelForCausalLM

        assert isinstance(device, str)
        assert isinstance(pretrained, str)
        assert isinstance(batch_size, (int,str))

        device_list = set(["cuda", "cpu"] + [f'cuda:{i}' for i in range(torch.cuda.device_count())])
        if device and device in device_list:
            self._device = torch.device(device)
            print(f"Using device '{device}'")
        else:
            print("Device not specified")
            print(f"Cuda Available? {torch.cuda.is_available()}")
            self._device = (
                torch.device("cuda")
                if torch.cuda.is_available()
                else torch.device("cpu")
            )

        # TODO: update this to be less of a hack once subfolder is fixed in HF
        revision = revision + ("/" + subfolder if subfolder is not None else "")

        ov_config = {"PERFORMANCE_HINT": "LATENCY", "NUM_STREAMS": "1", "CACHE_DIR": ""}

        self.gpt2 = OVModelForCausalLM.from_pretrained(
            pretrained,
            load_in_8bit=load_in_8bit,
            revision=revision,
            trust_remote_code=trust_remote_code,
            use_cache=True,
            ov_config=ov_config
        )

        try:
            self.tokenizer = transformers.AutoTokenizer.from_pretrained(
                pretrained if tokenizer is None else tokenizer,
                revision=revision,
                trust_remote_code=trust_remote_code,
            )
        except:
            print("Tokenizer is missed. Plaase save it into the same folder with the model.")

        self.vocab_size = self.tokenizer.vocab_size

        # setup for automatic batch size detection
        if batch_size == 'auto': 
            self.batch_size_per_gpu = batch_size
        else:
            self.batch_size_per_gpu = int(batch_size) 

    @property
    def eot_token_id(self):
        # we use EOT because end of *text* is more accurate for what we're doing than end of *sentence*
        return self.tokenizer.eos_token_id

    @property
    def max_length(self):
        try:
            return self.gpt2.config.n_ctx
        except AttributeError:
            # gptneoconfig doesn't have n_ctx apparently
            return self.gpt2.config.max_position_embeddings

    @property
    def max_gen_toks(self):
        return 256

    @property
    def batch_size(self):
        # TODO: fix multi-gpu
        return self.batch_size_per_gpu  # * gpus

    @property
    def device(self):
        # TODO: fix multi-gpu
        return self._device

    def tok_encode(self, string: str):
        return self.tokenizer.encode(string, add_special_tokens=False)

    def tok_decode(self, tokens):
        return self.tokenizer.decode(tokens)

    def _model_call(self, inps):
        """
        inps: a torch tensor of shape [batch, sequence]
        the size of sequence may vary from call to call

        returns: a torch tensor of shape [batch, sequence, vocab] with the
        logits returned from the model
        """
        return self.gpt2(inps)[0]

    def _model_generate(self, context, max_length, eos_token_id):
        generation_kwargs = {'do_sample': False, 'max_length': max_length}
        if eos_token_id is not None:
            generation_kwargs['eos_token_id'] = eos_token_id
            generation_kwargs['pad_token_id'] = eos_token_id # setting eos_token_id as pad token
        return self.gpt2.generate(context, **generation_kwargs)
