"""
Completions work with isolated strings of text pre and post generation.
"""

from __future__ import annotations

import asyncio
import string
import typing as t
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import runtime_checkable
from uuid import UUID, uuid4

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, computed_field

from rigging.error import CompletionExhaustedMaxRoundsError
from rigging.generator import GenerateParams, Generator, get_generator
from rigging.generator.base import StopReason, Usage  # noqa: TCH001
from rigging.parsing import parse_many

if t.TYPE_CHECKING:
    from rigging.model import Model, ModelT

DEFAULT_MAX_ROUNDS = 5

# TODO: Chats and Completions share a lot of structure and code.
# Ideally we should build out a base class which they both inherit from.


class Completion(BaseModel):
    """
    Represents a completed text generation.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    uuid: UUID = Field(default_factory=uuid4)
    """The unique identifier."""
    timestamp: datetime = Field(default_factory=datetime.now, repr=False)
    """The timestamp when the completion was created."""
    text: str
    """The original text."""
    generated: str
    """The generated text."""
    metadata: dict[str, t.Any] = Field(default_factory=dict)
    """Additional metadata for the completion."""

    stop_reason: StopReason = Field(default="unknown")
    """The reason the generation stopped."""
    usage: t.Optional[Usage] = Field(None, repr=False)
    """The usage statistics for the generation if available."""
    extra: dict[str, t.Any] = Field(default_factory=dict, repr=False)
    """Any additional information from the generation."""

    generator: t.Optional[Generator] = Field(None, exclude=True, repr=False)
    """The generator associated with the completion."""
    params: t.Optional[GenerateParams] = Field(None, exclude=True, repr=False)
    """Any additional generation params used for this completion."""

    failed: bool = Field(default=False, repr=False)
    """
    Indicates whether conditions during generation were not met.
    This is typically used for graceful error handling when parsing.
    """

    @computed_field(repr=False)  # type: ignore[misc]
    @property
    def generator_id(self) -> str | None:
        """The identifier of the generator used to create the completion"""
        if self.generator is not None:
            return self.generator.to_identifier(self.params)
        return None

    def __init__(
        self,
        text: str,
        generated: str,
        generator: t.Optional[Generator] = None,
        **kwargs: t.Any,
    ):
        """
        Initialize a Completion object.

        Args:
            text: The original text.
            generated: The generated text.
            generator: The generator associated with this completion.
            **kwargs: Additional keyword arguments (typically used for serialization).
        """
        if "generator_id" in kwargs and generator is None:
            # TODO: Should we move params to self.params?
            generator = get_generator(kwargs.pop("generator_id"))

        super().__init__(
            text=text,
            generated=generated,
            generator=generator,
            **kwargs,
        )

    def __len__(self) -> int:
        return len(self.text) + len(self.generated)

    @property
    def all(self) -> str:
        """Returns both the text and the generation."""
        return self.text + self.generated

    def restart(self, *, generator: t.Optional[Generator] = None, include_all: bool = False) -> CompletionPipeline:
        """
        Attempt to convert back to a CompletionPipeline for further generation.

        Args:
            generator: The generator to use for the restarted completion. Otherwise
                the generator from the original CompletionPipeline will be used.
            include_all: Whether to include the generation before the next round.
        Returns:
            The restarted completion.

        Raises:
            ValueError: If the completion was not created with a CompletionPipeline and no generator is provided.
        """

        text = self.all if include_all else self.generated
        if generator is None:
            generator = self.generator
        if generator is None:
            raise ValueError("Cannot restart a completion without an associated generator")
        return generator.complete(text, self.params)

    def fork(self, text: str, *, include_all: bool = False) -> CompletionPipeline:
        """
        Forks the completion by creating calling [rigging.completion.Completion.restart][] and appends the specified text.

        Args:
            text: The text to append.

        Returns:
            A new instance of the pipeline with the specified messages added.
        """
        return self.restart(include_all=include_all).add(text)

    def continue_(self, text: str) -> CompletionPipeline:
        """Alias for the [rigging.completion.Completion.fork][] with `include_all=True`."""
        return self.fork(text, include_all=True)

    def clone(self, *, only_messages: bool = False) -> Completion:
        """Creates a deep copy of the completion."""
        new = Completion(self.text, self.generated, self.generator)
        if not only_messages:
            new.metadata = deepcopy(self.metadata)
            new.stop_reason = self.stop_reason
            new.usage = self.usage.model_copy() if self.usage is not None else self.usage
            new.extra = deepcopy(self.extra)
            new.params = self.params.model_copy() if self.params is not None else self.params
            new.failed = self.failed
        return new

    def meta(self, **kwargs: t.Any) -> Completion:
        """
        Updates the metadata of the completion with the provided key-value pairs.

        Args:
            **kwargs: Key-value pairs representing the metadata to be updated.

        Returns:
            The updated completion object.
        """
        new = self.clone()
        new.metadata.update(kwargs)
        return new


# Callbacks


@runtime_checkable
class UntilCompletionCallback(t.Protocol):
    def __call__(self, text: str) -> bool:
        """
        A callback function that takes the generated text and returns whether or not to retry generation.
        """
        ...


@runtime_checkable
class ThenCompletionCallback(t.Protocol):
    async def __call__(self, completion: Completion) -> Completion | None:
        """
        Passed a finalized completion to process and can return a new completion to replace it.
        """
        ...


@runtime_checkable
class MapCompletionCallback(t.Protocol):
    async def __call__(self, completions: list[Completion]) -> list[Completion]:
        """
        Passed a finalized completion to process.

        This callback can replace, remove, or extend completions
        in the pipeline.
        """
        ...


@runtime_checkable
class WatchCompletionCallback(t.Protocol):
    async def __call__(self, completions: list[Completion]) -> None:
        """
        Passed any created completion objects for monitoring/logging.
        """
        ...


@dataclass
class RunState:
    text: str
    params: GenerateParams
    processor: t.Generator[None, str, str]
    completion: Completion | None = None
    watched: bool = False


class CompletionPipeline:
    """
    Pipeline to manipulate and produce completions.
    """

    def __init__(
        self,
        generator: Generator,
        text: str,
        *,
        params: t.Optional[GenerateParams] = None,
        watch_callbacks: t.Optional[list[WatchCompletionCallback]] = None,
    ):
        self.generator: Generator = generator
        """The generator object responsible for generating the completion."""
        self.text = text
        """The text to be completed."""
        self.params = params
        """The parameters for generating the completion."""
        self.metadata: dict[str, t.Any] = {}
        """Additional metadata associated with the completion."""

        # (callback, all_text, max_rounds)
        self.until_callbacks: list[tuple[UntilCompletionCallback, bool, int]] = []
        self.until_types: list[type[Model]] = []
        self.then_callbacks: list[ThenCompletionCallback] = []
        self.map_callbacks: list[MapCompletionCallback] = []
        self.watch_callbacks: list[WatchCompletionCallback] = watch_callbacks or []

    def __len__(self) -> int:
        return len(self.text)

    def with_(self, params: t.Optional[GenerateParams] = None, **kwargs: t.Any) -> CompletionPipeline:
        """
        Assign specific generation parameter overloads for this completion.

        Note:
            This will trigger a `clone` if overload params have already been set.

        Args:
            params: The parameters to set for the completion.
            **kwargs: An alternative way to pass parameters as keyword arguments.

        Returns:
            The current (or cloned) instance of the completion.
        """
        if params is None:
            params = GenerateParams(**kwargs)

        if self.params is not None:
            new = self.clone()
            new.params = self.params.merge_with(params)
            return new

        self.params = params
        return self

    def watch(self, *callbacks: WatchCompletionCallback, allow_duplicates: bool = False) -> CompletionPipeline:
        """
        Registers a callback to monitor any completions produced.

        Args:
            *callbacks: The callback functions to be executed.
            allow_duplicates: Whether to allow (seemingly) duplicate callbacks to be added.

        ```
        async def log(completions: list[Completion]) -> None:
            ...

        pipeline.watch(log).run()
        ```

        Returns:
            The current instance.
        """
        for callback in callbacks:
            if allow_duplicates or callback not in self.watch_callbacks:
                self.watch_callbacks.append(callback)
        return self

    def then(self, callback: ThenCompletionCallback) -> CompletionPipeline:
        """
        Registers a callback to be executed after the generation process completes.

        Note:
            Returning a Completion object from the callback will replace the current completion.
            for the remainder of the callbacks + return value of `run()`.

        ```
        async def process(completion: Completion) -> Completion | None:
            ...

        pipeline.then(process).run()
        ```

        Args:
            callback: The callback function to be executed.

        Returns:
            The current instance of the pipeline.
        """
        self.then_callbacks.append(callback)
        return self

    def map(self, callback: MapCompletionCallback) -> CompletionPipeline:
        """
        Registers a callback to be executed after the generation process completes.

        Note:
            You must return a list of completion objects from the callback which will
            represent the state of completions for the remainder of the callbacks and return.

        ```
        async def process(completions: list[Completion]) -> list[Completion]:
            ...

        pipeline.map(process).run()
        ```

        Args:
            callback: The callback function to be executed.

        Returns:
            The current instance of the completion.
        """
        self.map_callbacks.append(callback)
        return self

    def add(self, text: str) -> CompletionPipeline:
        """
        Appends new text to the internal text before generation.

        Args:
            text: The text to be added to the completion.

        Returns:
            The updated CompletionPipeline object.
        """
        self.text += text
        return self

    def fork(self, text: str) -> CompletionPipeline:
        """
        Creates a new instance of `CompletionPipeline` by forking the current completion and adding the specified text.

        This is a convenience method for calling `clone().add(text)`.

        Args:
            text: The text to be added to the new completion.

        Returns:
            A new instance of `CompletionPipeline` with the specified text added.
        """
        return self.clone().add(text)

    def clone(self, *, only_text: bool = False) -> CompletionPipeline:
        """
        Creates a clone of the current `CompletionPipeline` instance.

        Args:
            only_text: If True, only the text will be cloned.
                If False (default), the entire `CompletionPipeline` instance will be cloned
                including until callbacks, types, and metadata.

        Returns:
            A new instance of `CompletionPipeline` that is a clone of the current instance.
        """
        new = CompletionPipeline(
            self.generator,
            self.text,
            params=self.params.model_copy() if self.params is not None else None,
            watch_callbacks=self.watch_callbacks,
        )
        if not only_text:
            new.until_callbacks = self.until_callbacks.copy()
            new.until_types = self.until_types.copy()
            new.metadata = deepcopy(self.metadata)
            new.then_callbacks = self.then_callbacks.copy()
            new.map_callbacks = self.map_callbacks.copy()
        return new

    def meta(self, **kwargs: t.Any) -> CompletionPipeline:
        """
        Updates the metadata of the completion with the provided key-value pairs.

        Args:
            **kwargs: Key-value pairs representing the metadata to be updated.

        Returns:
            The updated completion object.
        """
        self.metadata.update(kwargs)
        return self

    def apply(self, **kwargs: str) -> CompletionPipeline:
        """
        Applies keyword arguments to the text using string template substitution.

        Note:
            This produces a clone of the CompletionPipeline, leaving the original unchanged.

        Args:
            **kwargs: Keyword arguments to be applied to the text.

        Returns:
            A new instance of CompletionPipeline with the applied arguments.
        """
        new = self.clone()
        template = string.Template(self.text)
        new.text = template.safe_substitute(**kwargs)
        return new

    def until(
        self,
        callback: UntilCompletionCallback,
        *,
        use_all_text: bool = False,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ) -> CompletionPipeline:
        """
        Registers a callback to participate in validating the generation process.

        ```py
        # Takes the generated text, and returns whether or not to retry generation.

        def callback(text: str) -> bool:
            if is_valid(text):
                return False
            else:
                return True

        pipeline.until(callback).run()
        ```

        Args:
            callback: The callback function to be executed.
            use_all_text: Whether to pass the entire text (including prompt) to the callback.

            max_rounds: The maximum number of rounds to attempt generation + callbacks
                before giving up.

        Returns:
            The current instance of the completion.
        """
        self.until_callbacks.append((callback, use_all_text, max_rounds))
        return self

    def until_parsed_as(
        self,
        *types: type[ModelT],
        use_all_text: bool = False,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
    ) -> CompletionPipeline:
        """
        Adds the specified types to the list of types which should successfully parse
        before the generation process completes.

        Args:
            *types: The type or types of models to wait for.
            use_all_text: Whether to pass the entire text (including prompt) to the parser.
            max_rounds: The maximum number of rounds to try to parse successfully.

        Returns:
            The updated CompletionPipeline object.
        """
        self.until_types += types
        if next((c for c in self.until_callbacks if c[0] == self._until_parse_callback), None) is None:
            self.until_callbacks.append((self._until_parse_callback, use_all_text, max_rounds))

        return self

    def _until_parse_callback(self, text: str) -> bool:
        try:
            parse_many(text, *self.until_types)
        except Exception:
            return True
        return False

    async def _watch_callback(self, completions: list[Completion]) -> None:
        coros = [callback(completions) for callback in self.watch_callbacks]
        await asyncio.gather(*coros)

    async def _post_run(self, completions: list[Completion]) -> list[Completion]:
        for map_callback in self.map_callbacks:
            completions = await map_callback(completions)

        for then_callback in self.then_callbacks:
            coros = [then_callback(chat) for chat in completions]
            new_completions = await asyncio.gather(*coros)
            completions = [new or chat for new, chat in zip(new_completions, completions)]

        return completions

    # TODO: It's opaque exactly how we should blend multiple
    # until callbacks together, so here is the current implementation:
    #
    # - We take the lowest max_rounds from all until_callbacks
    # - Each loop, we let every callback run, if any tell us to retry, we do
    # - If we leave the loop with should_retry still True, we raise an error
    # - Assuming every should_retry is False, we break out of the loop and return

    def _process(self) -> t.Generator[None, str, str]:
        # If there are no until_callbacks, we can just yield the text
        if not self.until_callbacks:
            generated = yield
            return generated

        lowest_max_rounds = min((c[2] for c in self.until_callbacks), default=1)

        current_round = 0
        should_retry = True
        while should_retry and current_round < lowest_max_rounds:
            current_round += 1
            generated = yield
            for callback, use_all_text, _ in self.until_callbacks:
                should_retry = callback(self.text + generated if use_all_text else generated)
                if should_retry:
                    continue

        if should_retry:
            logger.warning(f"Exhausted lowest max rounds ({lowest_max_rounds})")
            raise CompletionExhaustedMaxRoundsError(lowest_max_rounds, generated)

        return generated

    def _fit_params(
        self, count: int, params: t.Sequence[t.Optional[GenerateParams] | None] | None = None
    ) -> list[GenerateParams]:
        params = [None] * count if params is None else list(params)
        if len(params) != count:
            raise ValueError(f"The number of params must be {count}")
        if self.params is not None:
            params = [self.params.merge_with(p) for p in params]
        return [(p or GenerateParams()) for p in params]

    async def run(self, *, allow_failed: bool = False) -> Completion:
        """
        Execute the generation process to produce the final completion.

        Returns:
            The generated Completion.
        """
        chats = await self.run_many(1, include_failed=allow_failed)
        return chats[0]

    __call__ = run

    # Many messages

    async def run_many(
        self,
        count: int,
        *,
        params: t.Sequence[t.Optional[GenerateParams]] | None = None,
        skip_failed: bool = False,
        include_failed: bool = False,
    ) -> list[Completion]:
        """
        Executes the generation process multiple times with the same inputs.

        Parameters:
            count: The number of times to execute the generation process.
            params: A sequence of parameters to be used for each execution.
            skip_failed: Enable to ignore any max rounds errors and return only successful completions.

        Returns:
            A list of generatated Completions.
        """
        if skip_failed and include_failed:
            raise ValueError("Cannot use both skip_failed and include_failed")

        states: list[RunState] = [RunState(self.text, p, self._process()) for p in self._fit_params(count, params)]
        _ = [next(state.processor) for state in states]

        pending_states = states
        while pending_states:
            inbounds = await self.generator.generate_texts(
                [s.text for s in pending_states], [s.params for s in pending_states]
            )

            for inbound, state in zip(inbounds, pending_states):
                try:
                    state.processor.send(inbound.text)
                except StopIteration as stop:
                    state.completion = Completion(
                        self.text,
                        t.cast(str, stop.value),
                        generator=self.generator,
                        params=state.params,
                        metadata=self.metadata,
                        stop_reason=inbound.stop_reason,
                        usage=inbound.usage,
                        extra=inbound.extra,
                    )
                except CompletionExhaustedMaxRoundsError as exhausted:
                    if not skip_failed and not include_failed:
                        raise
                    state.completion = Completion(
                        self.text,
                        exhausted.completion,
                        generator=self.generator,
                        params=state.params,
                        metadata=self.metadata,
                        stop_reason=inbound.stop_reason,
                        usage=inbound.usage,
                        extra=inbound.extra,
                        failed=True,
                    )

            pending_states = [s for s in pending_states if s.completion is None]
            to_watch_states = [s for s in states if s.completion is not None and not s.watched]

            await self._watch_callback([s.completion for s in to_watch_states if s.completion is not None])

            for state in to_watch_states:
                state.watched = True

        if skip_failed:
            completions = [s.completion for s in states if s.completion is not None and not s.completion.failed]
        else:
            completions = [s.completion for s in states if s.completion is not None]

        return await self._post_run(completions)

    # Batch completions

    async def run_batch(
        self,
        many: t.Sequence[str],
        params: t.Sequence[t.Optional[GenerateParams]] | None = None,
        *,
        skip_failed: bool = False,
        include_failed: bool = False,
    ) -> list[Completion]:
        """
        Executes the generation process accross multiple input messages.

        Note:
            Anything already in this pending completion will be used as the `prefix` parameter
            to [rigging.generator.Generator.generate_messages][].

        Parameters:
            many: A sequence of texts to generate with.
            params: A sequence of parameters to be used for each text.
            skip_failed: Enable to ignore any max rounds errors and return only successful completions.

        Returns:
            A list of generatated Completions.
        """
        if skip_failed and include_failed:
            raise ValueError("Cannot use both skip_failed and include_failed")

        params = self._fit_params(len(many), params)
        states: list[RunState] = [RunState(m, p, self._process()) for m, p in zip(many, params)]
        _ = [next(state.processor) for state in states]

        pending_states = states
        while pending_states:
            inbounds = await self.generator.generate_texts(
                [self.text + s.text for s in pending_states],
                [s.params for s in pending_states],
            )

            for inbound, state in zip(inbounds, pending_states):
                try:
                    state.processor.send(inbound.text)
                except StopIteration as stop:
                    state.completion = Completion(
                        self.text,
                        t.cast(str, stop.value),
                        generator=self.generator,
                        params=state.params,
                        metadata=self.metadata,
                        stop_reason=inbound.stop_reason,
                        usage=inbound.usage,
                        extra=inbound.extra,
                    )
                except CompletionExhaustedMaxRoundsError as exhausted:
                    if not skip_failed and not include_failed:
                        raise
                    state.completion = Completion(
                        self.text,
                        exhausted.completion,
                        generator=self.generator,
                        params=state.params,
                        metadata=self.metadata,
                        stop_reason=inbound.stop_reason,
                        usage=inbound.usage,
                        extra=inbound.extra,
                        failed=True,
                    )

            pending_states = [s for s in pending_states if s.completion is None]
            to_watch_states = [s for s in states if s.completion is not None and not s.watched]

            await self._watch_callback([s.completion for s in to_watch_states if s.completion is not None])

            for state in to_watch_states:
                state.watched = True

        if skip_failed:
            completions = [s.completion for s in states if s.completion is not None and not s.completion.failed]
        else:
            completions = [s.completion for s in states if s.completion is not None]

        return await self._post_run(completions)
