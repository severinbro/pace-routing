"""Count class IDs without depending on a model runtime."""

from collections import Counter

GROUPS = ("gvi", "sky_ratio", "bsi", "sidewalk_ratio", "obstacle_ratio")


def mask_metrics(mask, manifest):
    histogram = Counter(mask.get_flattened_data())
    void = set(manifest["void_ids"])
    counts = {str(key): count for key, count in histogram.items() if key not in void}
    if not counts.keys() <= manifest["classes"].keys():
        raise ValueError("mask contains unknown classes")
    valid = sum(counts.values())
    ratios = {label: count / valid for label, count in counts.items()}
    groups = {}
    for name in GROUPS:
        ids = set(map(str, manifest["metric_groups"].get(name, [])))
        if not ids <= manifest["classes"].keys():
            raise ValueError("unknown class in metric group")
        groups[name] = sum(counts.get(label, 0) for label in ids) / valid if valid else None
    return {
        "class_counts": counts,
        "class_ratios_json": ratios,
        "valid_pixel_count": valid,
        "void_pixel_count": mask.width * mask.height - valid,
        **groups,
    }
