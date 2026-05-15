# MCP Tool Registry

A monorepo of MCP servers for Earth Observation (EO) workflows.

Current server examples:

- `servers/effis` - wildfire and remote sensing analysis
- `servers/eve_retrieval` - document retrieval
- `servers/serpapi` - web search 
- `servers/geocode` - geocode places
- `servers/trallie` - structured data extraction

## Earth Virtual Expert (EVE)

**Earth Virtual Expert (EVE)** aims to advance the use of Large Language Models (LLMs) within the Earth Observation (EO) and Earth Science (ES) community.

- Website: https://eve.philab.esa.int/  
- HuggingFace: https://huggingface.co/eve-esa
- Other repositories: https://github.com/eve-esa

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install only what you need:

```bash
pip install -r servers/<server-name>/requirements.txt
```

Example:

```bash
pip install -r servers/effis/requirements.txt
```

## Run any server locally

Most servers support MCP stdio and HTTP transport.

```bash
# stdio (for MCP clients)
python servers/<server-name>/server.py --transport stdio
```

## Add a new EO server

1. Create `servers/<new-server>/`
2. Add `server.py` (FastMCP entrypoint)
3. Add `requirements.txt`
4. Optionally add `.env.template` and `test.py`
5. Validate locally, then open a PR

## Contribute and Deploy Locally

You can already contribute and use the servers to deploy locally. For full contribution and deployment rules, see [CONTRIBUTING GUIDE](CONTRIBUTING.md).

## Early Access

If you want early access to our AgentCore deployment, get in contact with us at **eve@picampus-school.com** and we'll give you access before we make it publicly accessible.

## Funding

This project is supported by the European Space Agency (ESA) Φ-lab through the Large Language Model for Earth Observation and Earth Science project, as part of the Foresight Element within FutureEO Block 4 programme.

## Citation 

If you use this project in academic or research settings, please cite:
```
@misc{atrio2026evedomainspecificllmframework,
      title={{EVE}: A Domain-Specific {LLM} Framework for Earth Intelligence}, 
      author={Àlex R. Atrio and Antonio Lopez and Jino Rohit and Yassine El Ouahidi and Marcello Politi and Vijayasri Iyer and Umar Jamil and Sébastien Bratières and Nicolas Longépé},
      year={2026},
      eprint={2604.13071},
      archivePrefix={arXiv},
      primaryClass={cs.CL},
      url={https://arxiv.org/abs/2604.13071}, 
}
```

## License

This project is released under the Apache 2.0 License - see the [LICENSE](LICENSE) file for more details.