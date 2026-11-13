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


def _stream_product_plan(task):
    """Stream the read-only product-master agent and return its guarded plan.

    Module-level on purpose: ``codes/main.py`` calls every method on
    ``CommandChecker`` with no arguments while looking for a command match, so
    an undecorated helper method here would be invoked as a command.
    """
    from apps.core.services.AI.Agents.manager_product import ProductMasterAgent

    plan = None
    agent = ProductMasterAgent(mode="plan")
    try:
        for event in agent.stream(task):
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

        # last reviewed product-master plan (used by '/act' in the -mp command)
        self._last_product_plan = None

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

    # ---- product-master maintenance agent (LangChain + Ollama qwen3.5) ----
    # One command: type a task, optionally prefixed with /plan or /act.
    # /plan (or no token) streams a read-only plan and stores it; /act executes
    # the last reviewed plan (or a fresh plan when a task is supplied) after a
    # y/N confirmation, through the deterministic execute_plan guardrail.
    @command_check(["mp"])
    @params_check({"task": ["", str], "dry_run": [True, bool]})
    def product_master(self, **params):
        from apps.core.services.AI.Agents.manager_product import (
            format_plan,
            parse_mode,
            assert_plan_sql,
        )
        from apps.core.services.AI.Tools.act_tools import execute_plan
        from apps.core import config

        mode, task = parse_mode(params["task"])

        if mode == "plan":
            if not task:
                print(
                    "Task is empty. Type a task, optionally prefixed with /plan or /act."
                )
                return self.COMMAND_CHECKED
            plan = _stream_product_plan(task)
            if plan is not None:
                self._last_product_plan = plan
                print("\nHint: run '-mp' again and type '/act' to execute this plan.")
            return self.COMMAND_CHECKED

        # ---- /act mode ----
        if (
            str(config.get("AI_ALLOW_ACT", "0")).strip().lower()
            not in ("1", "true", "yes")
        ):
            print("act mode is disabled; set AI_ALLOW_ACT=1 in .env")
            return self.COMMAND_CHECKED

        if task:
            plan = _stream_product_plan(task)
            if plan is None:
                return self.COMMAND_CHECKED
        elif self._last_product_plan is not None:
            plan = self._last_product_plan
        else:
            print("No plan yet. Run '-mp' with /plan <task> or /act <task> first.")
            return self.COMMAND_CHECKED

        plan = assert_plan_sql(plan, mode="act")
        print(format_plan(plan))
        if not inputModel.user_confirm():
            print("Cancelled.")
            return self.COMMAND_CHECKED

        executed = execute_plan(plan, dry_run=bool(params["dry_run"]))
        print(format_plan(plan, executed))
        return self.COMMAND_CHECKED
