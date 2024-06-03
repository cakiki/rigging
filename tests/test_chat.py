from __future__ import annotations

import typing as t

import pytest

from rigging import Message, MessageDict, Model, attr, element
from rigging.chat import Chat, ChatPipeline
from rigging.error import MissingModelError
from rigging.generator import GenerateParams, get_generator


class Example(Model):
    content: str


class Person(Model):
    name: str = attr()
    age: int


class Address(Model):
    street: str = element()
    city: str = element()


def test_message_initialization() -> None:
    msg = Message("user", "Hello, world!")
    assert msg.role == "user"
    assert msg.content == "Hello, world!"
    assert len(msg.parts) == 0


def test_message_from_dict() -> None:
    msg_dict: MessageDict = {"role": "system", "content": "You are an AI assistant."}
    msg = Message.fit(msg_dict)
    assert msg.role == "system"
    assert msg.content == "You are an AI assistant."


def test_message_from_str() -> None:
    msg = Message.fit("Please say hello.")
    assert msg.role == "user"
    assert msg.content == "Please say hello."


def test_message_str_representation() -> None:
    msg = Message("assistant", "I am an AI assistant.")
    assert str(msg) == "[assistant]: I am an AI assistant."


def test_message_content_update_strips_parts() -> None:
    msg = Message("user", "<example>test</example>")
    msg.parse(Example)
    assert len(msg.parts) == 1

    msg.content = "new content"
    assert len(msg.parts) == 0
    assert msg.content == "new content"


def test_message_apply_template() -> None:
    msg = Message("user", "Hello, $name!")
    new = msg.apply(name="Alice")
    assert msg != new
    assert msg.content == "Hello, $name!"
    assert new.content == "Hello, Alice!"


def test_message_strip_model() -> None:
    msg = Message("user", "Some early content.<example>test</example><example>test2</example>")
    msg.parse_set(Example)
    assert len(msg.parts) == 2

    msg.strip(Example, fail_on_missing=True)
    assert len(msg.parts) == 0
    assert msg.content == "Some early content."


def test_message_parse_model() -> None:
    msg = Message("user", "<example>test</example>")
    example = msg.parse(Example)
    assert isinstance(example, Example)
    assert example.content == "test"


def test_message_restructure() -> None:
    msg = Message("user", "<example   >test</example>")
    msg.parse(Example)
    assert len(msg.parts) == 1
    assert msg.content == "<example>test</example>"


def test_message_parse_set_min() -> None:
    msg = Message("user", "<example>test1</example><example>test2</example>")
    with pytest.raises(MissingModelError):
        msg.parse_set(Example, minimum=3)


def test_message_parse_set() -> None:
    msg = Message("user", "<example>test1</example> <example>test2</example>")
    examples = msg.parse_set(Example, minimum=2)
    assert len(examples) == 2
    assert all(isinstance(e, Example) for e in examples)


def test_message_from_model() -> None:
    example = Example(content="test")
    msg = Message.from_model(example, role="assistant", suffix="Additional text")
    assert msg.role == "assistant"
    assert "<example>test</example>" in msg.content
    assert "Additional text" in msg.content
    assert len(msg.parts) == 1


def test_messages_fit_list() -> None:
    messages: t.Any = [{"role": "system", "content": "You are an AI assistant."}, Message("user", "Hello!")]
    fitted = Message.fit_as_list(messages)
    assert len(fitted) == 2
    assert isinstance(fitted[0], Message)
    assert isinstance(fitted[1], Message)


def test_message_parse_multiple_models() -> None:
    msg = Message(
        "user", "<person name='John'>30</person> <address><street>123 Main St</street><city>Anytown</city></address>"
    )
    person = msg.parse(Person)
    address = msg.parse(Address)

    assert isinstance(person, Person)
    assert person.name == "John"
    assert person.age == 30

    assert isinstance(address, Address)
    assert address.street == "123 Main St"
    assert address.city == "Anytown"


def test_message_try_parse_missing() -> None:
    msg = Message("user", "No models here")
    person = msg.try_parse(Person)
    assert person is None


def test_message_reparse_modified_content() -> None:
    msg = Message("user", "<person name='John'>30</person>")
    msg.parse(Person)

    msg.content = "<person name='Jane'>25</person>"
    person = msg.parse(Person)

    assert person.name == "Jane"
    assert person.age == 25


def test_chat_generator_id() -> None:
    generator = get_generator("gpt-3.5")
    chat = Chat([], generator=generator)
    assert chat.generator_id == "litellm!gpt-3.5"

    other = Chat([])
    assert other.generator_id is None


def test_chat_metadata() -> None:
    chat = Chat([]).meta(key="value")
    assert chat.metadata == {"key": "value"}


def test_chat_restart() -> None:
    chat = Chat(
        [
            Message("user", "Hello"),
            Message("assistant", "Hi there!"),
        ],
        [
            Message("user", "Other Stuff"),
        ],
        generator=get_generator("gpt-3.5"),
    )

    assert len(chat.restart()) == 2
    assert len(chat.restart(include_all=True)) == 3
    assert len(chat.continue_(Message("user", "User continue (should append)"))) == 3
    assert len(chat.continue_(Message("assistant", "Assistant continue"))) == 4

    chat.generator = None
    with pytest.raises(ValueError):
        chat.restart()


def test_chat_continue() -> None:
    chat = Chat(
        [
            Message("user", "Hello"),
            Message("assistant", "Hi there!"),
        ],
        generator=get_generator("gpt-3.5"),
    )

    continued = chat.continue_([Message("user", "How are you?")]).chat

    assert len(continued) == 3
    assert continued.all[0].content == "Hello"
    assert continued.all[1].content == "Hi there!"
    assert continued.all[2].content == "How are you?"


def test_chat_to_message_dicts() -> None:
    chat = Chat(
        [
            Message("user", "Hello"),
            Message("assistant", "Hi there!"),
        ],
        generator=get_generator("gpt-3.5"),
    )

    assert len(chat.message_dicts) == 2
    assert chat.message_dicts[0] == {"role": "user", "content": "Hello"}
    assert chat.message_dicts[1] == {"role": "assistant", "content": "Hi there!"}


def test_chat_to_conversation() -> None:
    chat = Chat(
        [
            Message("user", "Hello"),
            Message("assistant", "Hi there!"),
        ],
        generator=get_generator("gpt-3.5"),
    )

    assert "[user]: Hello" in chat.conversation
    assert "[assistant]: Hi there!" in chat.conversation


def test_chat_properties() -> None:
    user_1 = Message("user", "Hello")
    assistant_1 = Message("assistant", "Hi there!")
    user_2 = Message("user", "How are you?")
    assistant_2 = Message("assistant", "I'm doing well, thank you!")

    chat = Chat(
        [
            user_1,
        ],
        [assistant_1, user_2, assistant_2],
    )

    assert chat.prev == [user_1]
    assert chat.next == [assistant_1, user_2, assistant_2]
    assert chat.all == [user_1, assistant_1, user_2, assistant_2]
    assert chat.last == assistant_2


def test_chat_pipeline_continue() -> None:
    pipeline = ChatPipeline(get_generator("gpt-3.5"), [])
    continued = pipeline.fork([Message("user", "Hello")])

    assert continued != pipeline
    assert len(continued.chat) == 1
    assert continued.chat.all[0].content == "Hello"


def test_chat_pipeline_add() -> None:
    pipeline = ChatPipeline(get_generator("gpt-3.5"), [Message("user", "Hello")])
    added = pipeline.add(Message("user", "There"))

    assert added == pipeline
    assert len(added.chat) == 1
    assert added.chat.all[0].content == "Hello\nThere"

    diff_added = pipeline.add(Message("assistant", "Hi there!"))
    assert diff_added == added == pipeline
    assert len(diff_added.chat) == 2
    assert diff_added.chat.all[1].content == "Hi there!"


def test_chat_continue_maintains_parsed_models() -> None:
    chat = Chat(
        [
            Message("user", "<person name='John'>30</person>"),
            Message("assistant", "<address><street>123 Main St</street><city>Anytown</city></address>"),
        ],
        generator=get_generator("gpt-3.5"),
    )

    chat.all[0].parse(Person)
    chat.all[1].parse(Address)

    continued = chat.continue_([Message("user", "Additional message")]).chat

    assert len(continued.all[0].parts) == 1
    assert len(continued.all[1].parts) == 1
    assert len(continued.all[2].parts) == 0


def test_chat_pipeline_meta() -> None:
    pipeline = ChatPipeline(get_generator("gpt-3.5"), [Message("user", "Hello")])
    with_meta = pipeline.meta(key="value")
    assert with_meta == pipeline
    assert with_meta.metadata == {"key": "value"}


def test_chat_pipeline_with() -> None:
    pipeline = ChatPipeline(get_generator("gpt-3.5"), [Message("user", "Hello")])
    with_pipeline = pipeline.with_(GenerateParams(max_tokens=123))
    assert with_pipeline == pipeline
    assert with_pipeline.params is not None
    assert with_pipeline.params.max_tokens == 123

    with_pipeline_2 = with_pipeline.with_(GenerateParams(top_p=0.5))
    assert with_pipeline_2 != with_pipeline
    assert with_pipeline_2.params is not None
    assert with_pipeline_2.params.max_tokens == 123
    assert with_pipeline_2.params.top_p == 0.5


def test_chat_strip() -> None:
    chat = Chat(
        [
            Message("user", "<person name='John'>30</person>"),
            Message("assistant", "<address><street>123 Main St</street><city>Anytown</city></address>"),
        ]
    )

    assert len(chat) == 2

    chat.all[0].parse(Person)
    chat.all[1].parse(Address)

    stripped = chat.strip(Address)

    assert len(stripped.all[0].parts) == 1
    assert len(stripped.all[1].parts) == 0


def test_double_parse() -> None:
    msg = Message("user", "<person name='John'>30</person>")
    msg.parse(Person)
    msg.parse(Person)

    assert len(msg.parts) == 1


def test_double_parse_set() -> None:
    msg = Message(
        "user",
        "Some test content <anothertag><person  name='John'>30</person> More mixed content <person name='omad'>90</person><person   name='Jane'>25</person>",
    )
    existing_len = len(msg.content)
    msg.parse_set(Person)
    msg.parse_set(Person)

    assert len(msg.content) != existing_len
    assert len(msg.parts) == 3


def test_message_dedent() -> None:
    content = """\
        Tabbed content
        Line 2
    """

    message = Message("user", content)
    lines = message.content.split("\n")
    assert len(lines) == 3
    assert lines[0] == "Tabbed content"
    assert lines[1] == "Line 2"
    assert lines[2] == ""
