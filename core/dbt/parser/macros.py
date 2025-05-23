from typing import Iterable, List

import jinja2

from dbt.artifacts.resources.v1.macro import MacroArgument
from dbt.clients.jinja import get_supported_languages
from dbt.contracts.files import FilePath, SourceFile
from dbt.contracts.graph.nodes import Macro
from dbt.contracts.graph.unparsed import UnparsedMacro
from dbt.exceptions import ParsingError
from dbt.flags import get_flags
from dbt.node_types import NodeType
from dbt.parser.base import BaseParser
from dbt.parser.search import FileBlock, filesystem_search
from dbt_common.clients import jinja
from dbt_common.clients._jinja_blocks import ExtractWarning
from dbt_common.utils import MACRO_PREFIX


class MacroParser(BaseParser[Macro]):
    # This is only used when creating a MacroManifest separate
    # from the normal parsing flow.
    def get_paths(self) -> List[FilePath]:
        return filesystem_search(
            project=self.project, relative_dirs=self.project.macro_paths, extension=".sql"
        )

    @property
    def resource_type(self) -> NodeType:
        return NodeType.Macro

    @classmethod
    def get_compiled_path(cls, block: FileBlock):
        return block.path.relative_path

    def parse_macro(self, block: jinja.BlockTag, base_node: UnparsedMacro, name: str) -> Macro:
        unique_id = self.generate_unique_id(name)
        macro_sql = block.full_block or ""

        return Macro(
            path=base_node.path,
            macro_sql=macro_sql,
            original_file_path=base_node.original_file_path,
            package_name=base_node.package_name,
            resource_type=base_node.resource_type,
            name=name,
            unique_id=unique_id,
        )

    def parse_unparsed_macros(self, base_node: UnparsedMacro) -> Iterable[Macro]:
        # This is a bit of a hack to get the file path to the deprecation
        def wrap_handle_extract_warning(warning: ExtractWarning) -> None:
            self._handle_extract_warning(warning=warning, file=base_node.original_file_path)

        try:
            blocks: List[jinja.BlockTag] = [
                t
                for t in jinja.extract_toplevel_blocks(
                    base_node.raw_code,
                    allowed_blocks={"macro", "materialization", "test", "data_test"},
                    collect_raw_data=False,
                    warning_callback=wrap_handle_extract_warning,
                )
                if isinstance(t, jinja.BlockTag)
            ]
        except ParsingError as exc:
            exc.add_node(base_node)
            raise

        for block in blocks:
            try:
                ast = jinja.parse(block.full_block)
            except ParsingError as e:
                e.add_node(base_node)
                raise

            if (
                isinstance(ast, jinja2.nodes.Template)
                and hasattr(ast, "body")
                and len(ast.body) == 1
                and isinstance(ast.body[0], jinja2.nodes.Macro)
            ):
                # If the top level node in the Template is a Macro, things look
                # good and this is much faster than traversing the full ast, as
                # in the following else clause. It's not clear if that traversal
                # is ever really needed.
                macro = ast.body[0]
            else:
                macro_nodes = list(ast.find_all(jinja2.nodes.Macro))

                if len(macro_nodes) != 1:
                    # things have gone disastrously wrong, we thought we only
                    # parsed one block!
                    raise ParsingError(
                        f"Found multiple macros in {block.full_block}, expected 1", node=base_node
                    )

                macro = macro_nodes[0]

            if not macro.name.startswith(MACRO_PREFIX):
                continue

            name: str = macro.name.replace(MACRO_PREFIX, "")
            node = self.parse_macro(block, base_node, name)

            if getattr(get_flags(), "validate_macro_args", False):
                node.arguments = self._extract_args(macro)

            # get supported_languages for materialization macro
            if block.block_type_name == "materialization":
                node.supported_languages = get_supported_languages(macro)
            yield node

    def _extract_args(self, macro) -> List[MacroArgument]:
        try:
            return list([MacroArgument(name=arg.name) for arg in macro.args])
        except Exception:
            return []

    def parse_file(self, block: FileBlock):
        assert isinstance(block.file, SourceFile)
        source_file = block.file
        assert isinstance(source_file.contents, str)
        original_file_path = source_file.path.original_file_path

        # this is really only used for error messages
        base_node = UnparsedMacro(
            path=original_file_path,
            original_file_path=original_file_path,
            package_name=self.project.project_name,
            raw_code=source_file.contents,
            resource_type=NodeType.Macro,
            language="sql",
        )

        for node in self.parse_unparsed_macros(base_node):
            self.manifest.add_macro(block.file, node)
