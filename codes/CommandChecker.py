import os

from apps.core.services.chat_service import ChatController
from apps.core.services.price_service import PriceController
from apps.core.services.ollama_service import OllamaController
from apps.core.services.file_service import FileController
from apps.core.services.alibaba_service import AlibabaController
from apps.core.services.shipping_service import ShippingController
from apps.core.services.image_service import ImageController
from codes.controllers.PyputController import PyputController
from codes.controllers.CapScreenController import CapScreenController
from codes.controllers.WebController import WebController
from codes.controllers.CanvaAutoController import CanvaAutoController
from codes.utils import paramModel, timeModel, fileModel, inputModel, printModel
from codes.utils.paramModel import command_check, params_check


def _stream_turn(agent, task, domain=None):
    """Stream one turn of a running general-agent conversation.

    The agent owns the conversation memory, so follow-up questions keep their
    context automatically.  Returns the guarded plan, or ``None`` on failure.

    Module-level on purpose: ``codes/main.py`` calls every method on
    ``CommandChecker`` with no arguments while looking for a command match, so
    an undecorated helper method here would be invoked as a command.
    """
    plan = None
    try:
        for event in agent.stream(task, domain=domain):
            if event["type"] == "token":
                print(event["text"], end="", flush=True)
            elif event["type"] == "status":
                print(f"\n[{event['text']}]", flush=True)
            elif event["type"] == "error":
                print(f"\n[ERROR] {event['error']}", flush=True)
                return None
            elif event["type"] == "plan":
                plan = event["plan"]
                print("\n\n" + event["text"])
    except Exception as exc:  # noqa: BLE001 - surface as an error line
        print(f"\n[ERROR] {exc}", flush=True)
        return None
    return plan


def _interactive_repl(checker, agent, prompt, title):
    """Run the interactive multi-turn REPL for a general manager agent.

    The loop lives here (it owns ``input``/``print``); command parsing and the
    act execution live on the agent / :mod:`manager_general`.
    """
    from apps.core.services.AI.Agents.manager_general import (
        assert_plan_sql,
        format_plan,
        parse_command,
    )
    from apps.core import config

    # command_check leaves COMMAND_HIT set and only params_check clears it; this
    # command prompts inside its own loop instead, so reset it here.
    checker.COMMAND_HIT = False

    allow_act = str(config.get("AI_ALLOW_ACT", "0")).strip().lower() in (
        "1", "true", "yes",
    )
    dry_run = str(config.get("AI_ACT_DRY_RUN", "1")).strip().lower() in (
        "1", "true", "yes",
    )

    print(f"\n=== {title} agent ===")
    print("Type a request; the conversation continues until you type /q.")
    print("/q quit  |  /new reset context  |  /compact summarise  |  /dry on  |  /live off")
    print("Prefix /plan (default) or /act to execute this turn; /product or /sales to force a domain.\n")

    while True:
        try:
            raw = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            break

        cmd = parse_command(raw)

        if cmd.name == "empty":
            continue
        if cmd.name == "quit":
            break
        if cmd.name == "reset":
            agent.reset()
            print("[context cleared]")
            continue
        if cmd.name == "dry":
            dry_run = True
            print("[dry run on: writes are simulated]")
            continue
        if cmd.name == "live":
            dry_run = False
            print("[dry run OFF: /act writes will be applied]")
            continue
        if cmd.name == "help":
            print(
                "Type a request to plan. '/act <task>' plans then executes that "
                "turn; '/act' alone executes the last plan; '/compact' summarises "
                "the conversation; '/q' quits."
            )
            continue
        if cmd.name == "compact":
            info = agent.compact(force=True)
            if info.get("compacted"):
                print(f"[compact] {info['before']} -> {info['after']} messages "
                      f"({info['words']} words)")
            else:
                print("[compact] nothing to compact (already minimal)")
            continue

        # ---- message / act turn ----
        if not cmd.text:
            if cmd.mode != "act":
                print("Type a request, or /q to quit.")
                continue
            if getattr(agent, "last_plan", None) is None:
                print("No plan yet. Ask for a change first, then type '/act'.")
                continue
            plan = agent.last_plan
            print(format_plan(plan))
        else:
            plan = _stream_turn(agent, cmd.text, domain=cmd.domain)
            if plan is None:
                continue
            agent.last_plan = plan

        if cmd.mode != "act":
            continue

        if not allow_act:
            print("act mode is disabled; set AI_ALLOW_ACT=1 in .env")
            continue
        plan = assert_plan_sql(plan, mode="act")
        if not plan.get("sql"):
            print("\nNothing to execute: the plan has no SQL statements.")
            continue
        if not inputModel.user_confirm():
            print("Cancelled.")
            continue
        plan, executed = agent.act(plan, dry_run=dry_run)
        print(format_plan(plan, executed))

    # main.py breaks its dispatch loop only on CHECKED; returning anything else
    # (or None) would leave self.ans as None and crash the next command.
    return checker.COMMAND_CHECKED


class CommandChecker:

    def __init__(self):
        self.pyputController = PyputController()
        self.capScreenController = CapScreenController()
        self.chatController = ChatController()
        self.fileController = FileController()
        self.webController = WebController()
        self.imageController = ImageController()
        self.canvaAutoController = CanvaAutoController()
        self.shippingController = ShippingController()
        self.priceController = PriceController()
        self.alibabaController = AlibabaController()
        self.ollamaController = OllamaController()

        # constants
        self.COMMAND_CHECKED = "CHECKED"
        self.COMMAND_NOT_CHECKED = "NOT_CHECKED"
        self.ans = None  # being passed the command what user input
        self.COMMAND_HIT = False

        # variables
        self.Product_Index = ""

        # control variable
        self.QUIT = "quit"
        self.COMMAND_MODE = True

    @command_check([""])
    def empty_command(self):
        print("cannot input empty string")
        return self.COMMAND_CHECKED

    @command_check(["q"])
    def quit(self):
        return self.QUIT

    @command_check(["ask"])
    @params_check({"question": ["", str]})
    def ask_question(self, **params):
        response = self.chatController.get_simple_response(**params)
        system_message = response.choices[0].message.content
        print(system_message)
        return self.COMMAND_CHECKED

    # interactive chat session with the local ollama model
    @command_check(["ollachat"])
    def ollama_chat(self):
        self.ollamaController.clear_chat()
        print("Chatting with Ollama. Type 'q' to quit.")
        while True:
            question = input("You: ")
            if question.strip().lower() in ("q", "quit", ""):
                break
            answer = self.ollamaController.get_system_message(question)
            print(f"Ollama: {answer}")
        return self.COMMAND_CHECKED

    # capture the text and paste it onto translate.txt
    @command_check(["ct", "capt"])
    def capture_txt(self):
        print("First coordinate. ")
        firstCoord = self.pyputController.get_pos_by_leftClick()
        print("Second coordinate. ")
        secondCoord = self.pyputController.get_pos_by_leftClick()
        # cap screen
        filename = self.capScreenController.cap(
            firstCoord, secondCoord, path=os.getenv("IMAGE_TO_TEXT_PATH")
        )
        # transfer into txt
        txt = self.chatController.image_to_txt(filename)
        print(txt)
        fileModel.write_txt(
            os.getenv("IMAGE_TO_TEXT_PATH"), "transfer.txt", f"{txt}\n", "a"
        )
        return self.COMMAND_CHECKED

    # translate the text from translate.txt file
    @command_check(["tl"])
    def translate_text_file(self):
        txt = fileModel.read_text(os.getenv("IMAGE_TO_TEXT_PATH"), "transfer.txt")
        translated_txt = self.chatController.translate_content(
            txt, "Chinese", "English", "Simple Professional"
        )
        print(translated_txt)
        fileModel.write_txt(
            os.getenv("IMAGE_TO_TEXT_PATH"), "result.txt", f"{translated_txt}\n", "a"
        )
        return self.COMMAND_CHECKED

    # translate the product name
    @command_check(["tln"])
    @params_check(
        {
            "content": ["", str],
            "fromT": ["Chinese", str],
            "toT": ["English", str],
            "tone": ["Simple and Professional", str],
            "seo_filename": ["", str],
        }
    )
    def translate_product_name(self, **params):
        seo_txt = self.fileController.readSEOWords_Alibaba(params["seo_filename"])
        translated_txt = self.chatController.translate_product_name(
            params["content"], params["fromT"], params["toT"], params["tone"], seo_txt
        )
        print(translated_txt)
        return self.COMMAND_CHECKED

    # simple translate
    @command_check(["t"])
    def translate_user_text(self):
        print("Input the translate txt: ")
        txt = inputModel.txt_input()
        translated_txt = self.chatController.translate_content(
            txt, "Chinese", "English", "Simple Professional"
        )
        print(translated_txt)
        return self.COMMAND_CHECKED

    # capture and translate the text
    @command_check(["ctt", "captt"])
    def capture_txt_translate(self):
        print("First coordinate. ")
        firstCoord = self.pyputController.get_pos_by_leftClick()
        print("Second coordinate. ")
        secondCoord = self.pyputController.get_pos_by_leftClick()
        # cap screen
        filename = self.capScreenController.cap(
            firstCoord, secondCoord, path=os.getenv("IMAGE_TO_TEXT_PATH")
        )
        # transfer into txt
        txt = self.chatController.image_to_txt(filename)
        print(txt)
        translated_txt = self.chatController.translate_content(
            txt, "Chinese", "English", "Simple Professional"
        )
        print(translated_txt)
        fileModel.write_txt(
            os.getenv("IMAGE_TO_TEXT_PATH"), "result.txt", f"{translated_txt}\n", "a"
        )
        return self.COMMAND_CHECKED

    @command_check(["pos"])
    def get_pos(self):
        coord = self.pyputController.get_pos_by_leftClick()
        print(coord)
        return self.COMMAND_CHECKED

    @command_check(["kw"])
    @params_check({"content": ["", str]})
    def get_product_keyword(self, **params):
        keywords = self.chatController.generate_product_keywords(**params)
        print(keywords)
        return self.COMMAND_CHECKED

    @command_check(["des"])
    @params_check({"content": ["", str], "tone": ["business and professional", str]})
    def get_product_description(self, **params):
        description = self.chatController.generate_product_description(**params)
        print(description)
        return self.COMMAND_CHECKED

    # list out what you want in the email
    @command_check(["we"])
    @params_check({"emailNote": [[], list], "toT": ["English", str]})
    def write_email(self, **params):
        email_txt = self.chatController.write_email(**params)
        fileModel.write_txt(
            os.getenv("IMAGE_TO_TEXT_PATH"), "result.txt", f"----\n{email_txt}\n", "a"
        )
        print(email_txt)
        return self.COMMAND_CHECKED

    # create product folder
    @command_check(["pf"])
    @params_check({"folderName": ["", str]})
    def create_product_folder(self, **params):
        self.Product_Index = self.fileController.create_product_folder(**params)
        return self.COMMAND_CHECKED

    # download 1688 images and video
    @command_check(["d1688"])
    @params_check(
        {"website": ["", str], "product_id": ["", str], "quit_finally": [True, bool]}
    )
    def download_1688_src(self, **params):
        self.webController.download_1688_images(**params)
        return self.COMMAND_CHECKED

    # download 1688 images and video from html text
    @command_check(["t1688"])
    @params_check({"product_id": ["", str], "version": [2, int]})
    def download_1688_src_text(self, **params):
        # html = fileModel.read_text(os.getenv("DOCS"), "html.txt")
        self.webController.download_1688_images_from_html(
            params["product_id"], params["version"]
        )
        return self.COMMAND_CHECKED

    @command_check(["re"])
    def reopen_driver(self):
        self.webController.init_driver()
        return self.COMMAND_CHECKED

    # extract image text from deepseek API
    @command_check(["tf"])
    @params_check({"product_index": [r"", str]})
    def extract_texts(self, **params):
        # read display folder
        self.imageController.write_extracted_txt(params["product_index"], "display")
        # read description folder
        self.imageController.write_extracted_txt(params["product_index"], "description")
        return self.COMMAND_CHECKED

    # auto put the product images into canva
    @command_check(["bc"])
    @params_check(
        {
            "product_index": ["", str],
            "imagesType": ["", str],
            "renameImage": [False, bool],
        },
    )
    def buildcanva(self, **params):
        self.canvaAutoController.product_into_canva_attempts(
            params["product_index"], params["imagesType"], params["renameImage"]
        )
        return self.COMMAND_CHECKED

    # auto put the product images into canva
    @command_check(["bcs"])
    @params_check({"product_indexs": ["", list], "renameImage": [False, bool]})
    def buildcanvas(self, **params):
        for product_index in params["product_indexs"]:
            self.canvaAutoController.product_into_canva_attempts(
                product_index, "display", params["renameImage"]
            )
            self.canvaAutoController.product_into_canva_attempts(
                product_index, "description", params["renameImage"]
            )
        return self.COMMAND_CHECKED

    @command_check()
    def bbox(self):
        self.pyputController.get_bbox_from_clicks()
        return self.COMMAND_CHECKED

    @command_check()
    def zip(self):
        product_ids = [
            os.path.join(os.getenv("PRODUCT_FOLDER_PATH"), t.strip())
            for t in fileModel.read_text(
                r"./docs", "zip_to_agent_product_id.txt"
            ).split("\n")
            if len(t.strip()) > 0
        ]
        fileModel.zip_folders_combined(
            product_ids,
            os.path.join(
                os.getenv("PRODUCT_FOLDER_PATH"),
                f"PetProductImages_{timeModel.getTimeS(outputFormat='%Y%m%d')}.zip",
            ),
        )
        return self.COMMAND_CHECKED

    @command_check()
    @params_check(
        {
            "l": ["", float],
            "w": ["", float],
            "h": ["", float],
            "kg": ["", float],
            "min_pcs": [1, int],
            "max_pcs": ["", int],
        }
    )
    def packing(self, **params):
        self.shippingController.findPacking(**params)
        return self.COMMAND_CHECKED

    @command_check()
    @params_check(
        {
            "qtyString": ["10, 100, 500, 1000", str],
            "cost": ["", float],
            "profit_max": ["", float, "In %"],
            "profit_min": ["", float, "In %"],
        }
    )
    def price(self, **params):
        data = self.priceController.price_range(**params)
        printModel.print_dict(data)
        return self.COMMAND_CHECKED

    @command_check(["ris"])
    @params_check(
        {
            "product_folder_name": ["", str],
        }
    )
    def rename_images_by_similarity(self, **params):
        self.alibabaController.rename_edited_folder_image_by_similarity(**params)
        return self.COMMAND_CHECKED

    # ---- product-master / general manager agent (LangChain + Ollama qwen3.5) ----
    # Both commands run the same interactive REPL (see _interactive_repl): every
    # message stays in the same conversation until /q.  -mp is pinned to the
    # product domain (no routing); -mg routes each turn to product or sales.
    @command_check(["mp"])
    def product_master(self, **params):
        from apps.core.services.AI.Agents.manager_general import GeneralManagerAgent
        return _interactive_repl(
            self,
            GeneralManagerAgent(pinned="product"),
            prompt="mp> ",
            title="product-master",
        )

    @command_check(["mg", "general"])
    def general_manager(self, **params):
        from apps.core.services.AI.Agents.manager_general import GeneralManagerAgent
        return _interactive_repl(
            self,
            GeneralManagerAgent(),
            prompt="mg> ",
            title="general",
        )
