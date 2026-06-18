from brain.notes.links import extract_obsidian_links


def test_extract_obsidian_links_parses_target_alias_anchor_and_raw():
    assert extract_obsidian_links("[[MCP]] [[projetos/brain|Brain]] [[Hermes#Curadoria]]") == [
        {"target": "MCP", "alias": None, "anchor": None, "raw": "[[MCP]]"},
        {
            "target": "projetos/brain",
            "alias": "Brain",
            "anchor": None,
            "raw": "[[projetos/brain|Brain]]",
        },
        {
            "target": "Hermes",
            "alias": None,
            "anchor": "Curadoria",
            "raw": "[[Hermes#Curadoria]]",
        },
    ]


def test_extract_obsidian_links_trims_fields_and_skips_empty_targets():
    assert extract_obsidian_links(
        "[[|Alias]] [[#Heading]] [[   ]] [[ MCP | protocolo ]] [[ Hermes # Curadoria ]]"
    ) == [
        {"target": "MCP", "alias": "protocolo", "anchor": None, "raw": "[[ MCP | protocolo ]]"},
        {
            "target": "Hermes",
            "alias": None,
            "anchor": "Curadoria",
            "raw": "[[ Hermes # Curadoria ]]",
        },
    ]
