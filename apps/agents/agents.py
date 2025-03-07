"""
Gradio-based web app Agents that uses OpenAI API to generate
a chat between collaborative agents.
"""

import argparse
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

import gradio as gr
import openai
import openai.error
import tenacity

from apps.agents.text_utils import split_markdown_code
from camel.agents import RolePlaying, TaskSpecifyAgent
from camel.messages import AssistantChatMessage

REPO_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

ChatBotHistory = List[Tuple[Optional[str], Optional[str]]]


@dataclass
class State:
    session: Optional[RolePlaying]
    max_messages: int
    chat: ChatBotHistory
    saved_assistant_msg: Optional[AssistantChatMessage]

    @classmethod
    def empty(cls) -> 'State':
        return cls(None, 0, [], None)

    @staticmethod
    def construct_inplace(
            state: 'State', session: Optional[RolePlaying], max_messages: int,
            chat: ChatBotHistory,
            saved_assistant_msg: Optional[AssistantChatMessage]) -> None:
        state.session = session
        state.max_messages = max_messages
        state.chat = chat
        state.saved_assistant_msg = saved_assistant_msg


def parse_arguments():
    """ Get command line arguments. """

    parser = argparse.ArgumentParser("Camel data explorer")
    parser.add_argument('--api-key', type=str, default=None,
                        help='OpenAI API key')
    parser.add_argument('--share', type=bool, default=False,
                        help='Expose the web UI to Gradio')
    parser.add_argument('--server-port', type=int, default=8080,
                        help='Port ot run the web page on')
    parser.add_argument('--inbrowser', type=bool, default=False,
                        help='Open the web UI in the default browser on lunch')
    parser.add_argument(
        '--concurrency-count', type=int, default=1,
        help='Number if concurrent threads at Gradio websocket queue. ' +
        'Increase to serve more requests but keep an eye on RAM usage.')
    args, unknown = parser.parse_known_args()
    if len(unknown) > 0:
        print("Unknown args: ", unknown)
    return args


def load_roles(path: str) -> List[str]:
    """ Load roles from list files.

    Args:
        path (str): Path to the TXT file.

    Returns:
        List[str]: List of roles.
    """

    assert os.path.exists(path)
    roles = []
    with open(path, "r") as f:
        lines = f.readlines()
        for line in lines:
            match = re.search(r"^\d+\.\s*(.+)\n*$", line)
            if match:
                role = match.group(1)
                roles.append(role)
            else:
                print("Warning: no match")
    return roles


def cleanup_on_launch(state) -> Tuple[State, ChatBotHistory, Dict]:
    """ Prepare the UI for a new session.

    Args:
        state (State): Role playing state.

    Returns:
        Tuple[State, ChatBotHistory, Dict]:
            - Updated state.
            - Chatbot window contents.
            - Start button state (disabled).
    """
    # The line below breaks the every=N runner
    # `state = State.empty()`

    State.construct_inplace(state, None, 0, [], None)

    return state, [], gr.update(interactive=False)


def role_playing_start(
    state,
    assistant: str,
    user: str,
    original_task: str,
    max_messages: float,
    with_task_specifier: bool,
    word_limit: int,
) -> Union[Dict, Tuple[State, str, Union[str, Dict], ChatBotHistory, Dict]]:
    """ Creates a role playing session.

    Args:
        state (State): Role playing state.
        assistant (str): Contents of the Assistant field.
        user (str): Contents of the User field.
        original_task (str): Original task field.
        with_task_specifier (bool): Enable/Disable task specifier.
        word_limit (int): Limit of words for task specifier.

    Returns:
        Union[Dict, Tuple[State, str, Union[str, Dict], ChatBotHistory, Dict]]:
            - Updated state.
            - Generated specified task.
            - Planned task (if any).
            - Chatbot window contents.
            - Progress bar contents.
    """

    if state.session is not None:
        print("Double click")
        return {}  # may fail

    try:
        task_specify_kwargs = dict(word_limit=word_limit) \
            if with_task_specifier else None

        session = RolePlaying(assistant, user, original_task,
                              with_task_specify=with_task_specifier,
                              task_specify_agent_kwargs=task_specify_kwargs,
                              with_task_planner=False)
    except (openai.error.RateLimitError, tenacity.RetryError,
            RuntimeError) as ex:
        print("OpenAI API exception 0 " + str(ex))
        return (state, str(ex), "", [], gr.update())

    # Can't re-create a state like below since it
    # breaks 'role_playing_chat_cont' runner with every=N.
    # `state = State(session=session, max_messages=int(max_messages), chat=[],`
    # `             saved_assistant_msg=None)`

    State.construct_inplace(state, session, int(max_messages), [], None)

    specified_task_prompt = session.specified_task_prompt \
        if session.specified_task_prompt is not None else ""
    planned_task_prompt = session.planned_task_prompt \
        if session.planned_task_prompt is not None else ""

    planned_task_upd = gr.update(
        value=planned_task_prompt, visible=session.planned_task_prompt
        is not None)

    progress_update = gr.update(maximum=state.max_messages, value=1,
                                visible=True)

    return (state, specified_task_prompt, planned_task_upd, state.chat,
            progress_update)


def role_playing_chat_init(state) -> \
        Union[Dict, Tuple[State, ChatBotHistory, Dict]]:
    """ Initialize role playing.

    Args:
        state (State): Role playing state.

    Returns:
        Union[Dict, Tuple[State, ChatBotHistory, Dict]]:
            - Updated state.
            - Chatbot window contents.
            - Progress bar contents.
    """

    if state.session is None:
        print("Error: session is none on role_playing_chat_init call")
        return state, state.chat, gr.update()

    try:
        assistant_msg, _ = state.session.init_chat()
        assistant_msg: AssistantChatMessage
    except (openai.error.RateLimitError, tenacity.RetryError,
            RuntimeError) as ex:
        print("OpenAI API exception 1 " + str(ex))
        state.session = None
        return state, state.chat, gr.update()

    state.saved_assistant_msg = assistant_msg

    progress_update = gr.update(maximum=state.max_messages, value=1,
                                visible=True)

    return state, state.chat, progress_update


# WORKAROUND: do not add type hinst for session and chatbot_histoty
def role_playing_chat_cont(state) -> \
        Tuple[State, ChatBotHistory, Dict, Dict]:
    """ Produce a pair of messages by an assistant and a user.
        To be run multiple times.

    Args:
        state (State): Role playing state.

    Returns:
        Union[Dict, Tuple[State, ChatBotHistory, Dict]]:
            - Updated state.
            - Chatbot window contents.
            - Progress bar contents.
            - Start button state (to be eventually enabled).
    """

    if state.session is None:
        return state, state.chat, gr.update(visible=False), gr.update()

    if state.saved_assistant_msg is None:
        return state, state.chat, gr.update(), gr.update()

    try:
        assistant_msgs, user_msgs = state.session.step(
            state.saved_assistant_msg)
    except (openai.error.RateLimitError, tenacity.RetryError,
            RuntimeError) as ex:
        print("OpenAI API exception 2 " + str(ex))
        state.session = None
        return state, state.chat, gr.update(), gr.update()

    u_msg = user_msgs[0]
    a_msg = assistant_msgs[0]

    state.saved_assistant_msg = a_msg

    state.chat.append((None, split_markdown_code(u_msg.content)))
    state.chat.append((split_markdown_code(a_msg.content), None))

    if len(state.chat) >= state.max_messages:
        state.session = None

    if "CAMEL_TASK_DONE" in a_msg.content or \
            "CAMEL_TASK_DONE" in u_msg.content:
        state.session = None

    progress_update = gr.update(maximum=state.max_messages,
                                value=len(state.chat), visible=state.session
                                is not None)

    start_bn_update = gr.update(interactive=state.session is None)

    return state, state.chat, progress_update, start_bn_update


def stop_session(state) -> Tuple[State, Dict, Dict]:
    """ Finish the session and leave chat contents as an artefact.

    Args:
        state (State): Role playing state.

    Returns:
        Union[Dict, Tuple[State, ChatBotHistory, Dict]]:
            - Updated state.
            - Progress bar contents.
            - Start button state (to be eventually enabled).
    """

    state.session = None
    return state, gr.update(visible=False), gr.update(interactive=True)


def construct_ui(blocks, api_key: Optional[str] = None) -> None:
    """ Build Gradio UI and populate with topics.

    Args:
        api_key (str): OpenAI API key.

    Returns:
        None
    """

    if api_key is not None:
        openai.api_key = api_key

    assistant_role_path = \
        os.path.join(REPO_ROOT, "data/ai_society/assistant_roles.txt")
    user_role_path = \
        os.path.join(REPO_ROOT, "data/ai_society/user_roles.txt")

    assistant_roles = load_roles(assistant_role_path)
    user_roles = load_roles(user_role_path)

    assistant_role = "Python Programmer"
    user_role = "Stock Trader"

    default_task = "Develop a trading bot for the stock market"

    with gr.Row():
        with gr.Column(scale=1):
            assistant_dd = gr.Dropdown(assistant_roles,
                                       label="Example assistant roles",
                                       value=assistant_role, interactive=True)
            assistant_ta = gr.TextArea(label="Assistant role (EDIT ME)",
                                       lines=1, interactive=True)
        with gr.Column(scale=1):
            user_dd = gr.Dropdown(user_roles, label="Example user roles",
                                  value=user_role, interactive=True)
            user_ta = gr.TextArea(label="User role (EDIT ME)", lines=1,
                                  interactive=True)
        with gr.Column(scale=1):
            gr.Markdown(
                "## CAMEL: Communicative Agents for \"Mind\" Exploration"
                " of Large Scale Language Model Society\n"
                "Github repo: [https://github.com/lightaime/camel]"
                "(https://github.com/lightaime/camel)"
                '<div style="display:flex; justify-content:center;">'
                '<img src="https://raw.githubusercontent.com/lightaime/camel/'
                'master/misc/logo.png" alt="Logo" style="max-width:50%;">'
                '</div>')
    with gr.Row():
        with gr.Column(scale=9):
            original_task_ta = gr.TextArea(
                label="Give me a preliminary idea (EDIT ME)",
                value=default_task, lines=1, interactive=True)
        with gr.Column(scale=1):
            universal_task_bn = gr.Button("Insert universal task")
    with gr.Row():
        with gr.Column():
            with gr.Row():
                task_specifier_cb = gr.Checkbox(value=True,
                                                label="With task specifier")
            with gr.Row():
                ts_word_limit_nb = gr.Number(
                    value=TaskSpecifyAgent.DEFAULT_WORD_LIMIT,
                    label="Word limit for task specifier",
                    visible=task_specifier_cb.value)
        with gr.Column():
            num_messages_sl = gr.Slider(minimum=1, maximum=50, step=1,
                                        value=10, interactive=True,
                                        label="Messages to generate")

        with gr.Column(scale=2):
            with gr.Row():
                start_bn = gr.Button("Make agents chat [takes time]",
                                     elem_id="start_button")
            with gr.Row():
                clear_bn = gr.Button("Interrupt the current query")
    progress_sl = gr.Slider(minimum=0, maximum=100, value=0, step=1,
                            label="Progress", interactive=False, visible=False)
    specified_task_ta = gr.TextArea(
        label="Specified task prompt given to the role-playing session"
        " based on the original (simplistic) idea", lines=1, interactive=False)
    task_prompt_ta = gr.TextArea(label="Planned task prompt", lines=1,
                                 interactive=False, visible=False)
    chatbot = gr.Chatbot(label="Chat between autonomous agents")
    session_state = gr.State(State.empty())

    universal_task_bn.click(lambda: "Help me to do my job", None,
                            original_task_ta)

    task_specifier_cb.change(lambda v: gr.update(visible=v), task_specifier_cb,
                             ts_word_limit_nb)

    start_bn.click(cleanup_on_launch, session_state,
                   [session_state, chatbot, start_bn], queue=False) \
            .then(role_playing_start,
                  [session_state, assistant_ta, user_ta,
                   original_task_ta, num_messages_sl,
                   task_specifier_cb, ts_word_limit_nb],
                  [session_state, specified_task_ta, task_prompt_ta,
                   chatbot, progress_sl],
                  queue=False) \
            .then(role_playing_chat_init, session_state,
                  [session_state, chatbot, progress_sl], queue=False)

    blocks.load(role_playing_chat_cont, session_state,
                [session_state, chatbot, progress_sl, start_bn], every=0.5)

    clear_bn.click(stop_session, session_state,
                   [session_state, progress_sl, start_bn])

    assistant_dd.change(lambda dd: dd, assistant_dd, assistant_ta)
    user_dd.change(lambda dd: dd, user_dd, user_ta)

    blocks.load(lambda dd: dd, assistant_dd, assistant_ta)
    blocks.load(lambda dd: dd, user_dd, user_ta)


def construct_blocks(api_key: Optional[str]):
    """ Construct Agents app but do not launch it.

    Args:
        api_key (Optional[str]): OpenAI API key.

    Returns:
        gr.Blocks: Blocks instance.
    """

    css_str = "#start_button {border: 3px solid #4CAF50; font-size: 20px;}"

    with gr.Blocks(css=css_str) as blocks:
        construct_ui(blocks, api_key)

    return blocks


def main():
    """ Entry point. """

    args = parse_arguments()

    print("Getting Agents web server online...")

    blocks = construct_blocks(args.api_key)

    blocks.queue(args.concurrency_count) \
          .launch(share=args.share, inbrowser=args.inbrowser,
                  server_name="0.0.0.0", server_port=args.server_port,
                  debug=True)

    print("Exiting.")


if __name__ == "__main__":
    main()
