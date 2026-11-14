from rest_framework.exceptions import ParseError
from rest_framework.parsers import BaseParser
from rest_framework.renderers import BaseRenderer

from .importers import ImportFormatError, parse_xml


class XMLOrders(list):
    """Seznam objednávek načtených z XML (odliší XML vstup od JSON)."""


class XMLOrdersParser(BaseParser):
    media_type = "application/xml"

    def parse(self, stream, media_type=None, parser_context=None):
        body = stream.read() if stream is not None else b""
        if not body.strip():
            raise ParseError("Prázdné tělo požadavku.")
        try:
            return XMLOrders(parse_xml(body))
        except ImportFormatError as exc:
            raise ParseError(str(exc)) from exc


class TextXMLOrdersParser(XMLOrdersParser):
    media_type = "text/xml"


class CSVRenderer(BaseRenderer):
    """Umožní klientům posílat ``Accept: text/csv`` (data skládá view sám)."""

    media_type = "text/csv"
    format = "csv"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        if data is None:
            return b""
        if isinstance(data, bytes | str):
            return data
        # Chybové odpovědi (např. 401) vrátíme jako prostý text.
        detail = data.get("detail", data) if isinstance(data, dict) else data
        return str(detail).encode(self.charset)
