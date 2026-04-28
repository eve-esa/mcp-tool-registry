
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from trallie.data_extraction.data_extractor import DataExtractor
from trallie.schema_generation.schema_generator import SchemaGenerator

load_dotenv()

DEFAULT_PROVIDER = "groq"
DEFAULT_MODEL = "llama-3.3-70b-versatile"

mcp = FastMCP("Trallie MCP", host="0.0.0.0", port=8000, stateless_http=True)


def get_fallback(value, default):
    return value if value else default


@mcp.tool()
def extract_data(
    description: str,
    records: list[str],
    provider: str | None = None,
    model_name: str | None = None,
    schema_fields: list[str] | None = None,
    return_schema: bool = True,
):
    """
    Performs Open Information Extraction (OpenIE) from a list of input texts using a language model.

    This function extracts structured data from unstructured text records based on a schema.
    The schema can either be provided or automatically discovered using the model.

    Args:
        description (str):
            A natural language description of what information to extract.
            This helps guide schema discovery if `schema_fields`is not provided.
        records (List[str]):
            A list of raw text inputs from which information is to be extracted.
        provider (Optional[str], default="openai"):
            The name of the language model provider (e.g., "openai", "anthropic").
            If not specified, defaults to the value of `DEFAULT_PROVIDER`.
        model_name (Optional[str], default="gpt-4o"):
            The name of the specific model to use from the selected provider.
            Defaults to `DEFAULT_MODEL` if not supplied.
        schema_fields (Optional[List[str]], default=None):
            A list of field names representing the schema to extract.
            If None, the schema is automatically generated using the `SchemaGenerator`
            based on the `description` and the input `records`.
        return_schema (bool, default=True):
            Whether to return the schema alongside the extracted data.
            If True, returns a dictionary with both the schema and extracted results.
            If False, returns only the extracted data list.

    Returns:
        Union[Dict[str, Any], List[Dict[str, Any]]]:
            If `return_schema` is True:
                {
                    "schema": List[str],        # The list of extracted field names
                    "extracted_data": List[Dict[str, Any]]  # The extracted values for each record
                }
            Else:
                List[Dict[str, Any]]  # Only the extracted values per input record
    """


    provider = get_fallback(provider, DEFAULT_PROVIDER)
    model_name = get_fallback(model_name, DEFAULT_MODEL)

    data_extractor = DataExtractor(provider=provider, model_name=model_name)

    # Step 1: Use provided schema or discover one
    if schema_fields is None:
        schema_generator = SchemaGenerator(provider=provider, model_name=model_name)
        schema_fields = schema_generator.discover_schema(description, records, from_text=True)

    # Step 2: Extract data
    extracted = [
        data_extractor.extract_data(schema_fields, record, from_text=True)
        for record in records
    ]

    return {
        "schema": schema_fields,
        "extracted_data": extracted
    } if return_schema else extracted


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SerpAPI MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="streamable-http",
        help="Transport type (default: streamable-http)",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transport")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host for HTTP transport")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
