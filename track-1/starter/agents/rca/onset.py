"""Sample-aware metric changes. No dataset reads or root-cause decisions here."""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np


DEFAULTS = {
    "summary_version": 2,
    "robust_threshold": 4.0,
    "relative_change_floor": 0.2,
    "gap_cadences": 2.5,
    "minimum_sustained_samples": 2,
    "spike_threshold": 6.0,
    "score_cap": 20.0,
    "minimum_reference_samples": 3,
}


def prepare_samples(samples: Iterable[tuple[float, float]], *, semantics: str = "unknown"):
    """Sort, combine duplicate timestamps and optionally difference known counters.

    Counter resets discard the ambiguous reset interval rather than inventing work.
    The raw counter remains represented by its source records and query.
    """
    if semantics not in {"unknown", "gauge", "counter"}:
        raise ValueError("semantics must be unknown, gauge or counter")
    valid = []
    invalid = 0
    for timestamp, value in samples:
        try:
            pair = float(timestamp), float(value)
        except (TypeError, ValueError):
            invalid += 1
            continue
        if not all(math.isfinite(x) for x in pair):
            invalid += 1
            continue
        valid.append(pair)
    valid.sort()
    grouped: dict[float, list[float]] = {}
    for timestamp, value in valid:
        grouped.setdefault(timestamp, []).append(value)
    arr = np.array([(t, float(np.median(v))) for t, v in grouped.items()], dtype=float)
    if not len(arr):
        arr = np.empty((0, 2), dtype=float)
    resets = 0
    if semantics == "counter" and len(arr):
        rates = []
        for previous, current in zip(arr, arr[1:]):
            delta = current[1] - previous[1]
            if delta < 0:
                resets += 1
                continue
            rates.append((current[0], float(delta / (current[0] - previous[0]))))
        arr = np.array(rates, dtype=float).reshape((-1, 2))
    return arr, {"invalid_samples": invalid, "duplicate_samples": len(valid) - len(grouped),
                 "counter_resets": resets}


def summarize_series(samples, start: float, end: float, *, semantics="unknown", params=None):
    """Return finite JSON values and all sampled episodes in [start, end).

    A zero MAD follows a bounded relative-change path. Missing baseline or sparse
    sampling never becomes a claim of normality. First change and peak are distinct.
    """
    config = dict(DEFAULTS)
    if params:
        config.update(params)
        # Saved v1 records remain exactly replayable with their saved settings.
        if "summary_version" not in params:
            config["summary_version"] = 1
    arr, quality = prepare_samples(samples, semantics=semantics)
    times, values = arr[:, 0], arr[:, 1]
    base = values[times < start]
    mask = (times >= start) & (times < end)
    incident_times, incident = times[mask], values[mask]
    differences = np.diff(times)
    cadence = float(np.median(differences[differences > 0])) if np.any(differences > 0) else None
    median = float(np.median(base)) if len(base) else None
    mad = float(np.median(np.abs(base - median))) if len(base) else None
    first_half = base[:len(base) // 2]
    second_half = base[len(base) // 2:]
    earlier = float(np.median(first_half)) if len(first_half) else None
    recent = float(np.median(second_half)) if len(second_half) else None
    result = {
        "baseline_n": int(len(base)), "baseline_median": median, "baseline_mad": mad,
        "baseline_earlier_median": earlier, "baseline_recent_median": recent,
        "window_n": int(len(incident)),
        "window_min": float(np.min(incident)) if len(incident) else None,
        "window_max": float(np.max(incident)) if len(incident) else None,
        "window_median": float(np.median(incident)) if len(incident) else None,
        "sampling_interval_s": cadence,
        "largest_gap_s": float(np.max(differences)) if len(differences) else None,
        "gap_count": int(np.sum(differences > config["gap_cadences"] * cadence)) if cadence else 0,
        "semantics": semantics, "reference_sensitive": None,
        "peak_timestamp_s": None, "peak_value": None, "strength": None,
        "anomalous_samples": None, "episodes": [], **quality,
    }
    if config["summary_version"] >= 2:
        result["baseline_p10"] = float(np.quantile(base, .1)) if len(base) else None
        result["baseline_p90"] = float(np.quantile(base, .9)) if len(base) else None
    if not len(base) or not len(incident):
        return result
    distance = np.abs(incident - median)
    max_distance = float(np.max(distance))
    # No arbitrary epsilon denominator: the constant-reference branch scales to
    # observed magnitude, so stable-zero -> nonzero scores at most 10.
    if mad == 0:
        scale = max(abs(median), float(np.max(np.abs(incident)))) / 10.0
    else:
        scale = max(1.4826 * mad, abs(median) * config["relative_change_floor"] / config["robust_threshold"])
    threshold = max(config["robust_threshold"] * scale if mad else 0.0,
                    config["relative_change_floor"] * abs(median))
    if mad == 0:
        magnitude = np.maximum(abs(median), np.abs(incident))
        scores = np.divide(10.0 * distance, magnitude, out=np.zeros_like(distance), where=magnitude != 0)
        scores = np.minimum(scores, config["score_cap"])
        anomalous = distance > threshold
    else:
        scores = np.minimum(distance / scale, config["score_cap"]) if scale else np.zeros(len(incident))
        anomalous = (distance > threshold) & (scores >= config["robust_threshold"])
    peak_index = int(np.argmax(distance))
    result.update(peak_timestamp_s=float(incident_times[peak_index]), peak_value=float(incident[peak_index]),
                  strength=float(np.max(scores)), anomalous_samples=int(np.sum(anomalous)))
    if earlier is not None and recent is not None:
        result["reference_sensitive"] = bool(abs(earlier - recent) > max(abs(median) * config["relative_change_floor"], mad, max_distance * 0.25))
    episodes = []
    index = 0
    while index < len(incident):
        if not anomalous[index]:
            index += 1
            continue
        first = index
        direction = "increase" if incident[index] > median else "decrease"
        while index + 1 < len(incident) and anomalous[index + 1]:
            next_direction = "increase" if incident[index + 1] > median else "decrease"
            if next_direction != direction or (cadence and incident_times[index + 1] - incident_times[index] > config["gap_cadences"] * cadence):
                break
            index += 1
        last = index
        count = last - first + 1
        strength = float(np.max(scores[first:last + 1]))
        sustained = count >= config["minimum_sustained_samples"]
        if sustained or strength >= config["spike_threshold"]:
            upper = float(incident_times[first])
            normal = arr[(times < upper) & (np.abs(values - median) <= threshold)]
            lower = float(normal[-1, 0]) if len(normal) else None
            gap_before = bool(lower is not None and cadence and upper - lower > config["gap_cadences"] * cadence)
            # An anomaly already present in the reference has no defensible onset.
            onset = [lower, upper] if lower is not None else None
            estimate = max(start, (lower + upper) / 2.0) if onset else None
            peak = first + int(np.argmax(distance[first:last + 1]))
            episodes.append({"first_change_s": upper, "last_change_s": float(incident_times[last]),
                             "onset_interval_s": onset, "onset_estimate_s": estimate,
                             "peak_timestamp_s": float(incident_times[peak]), "peak_value": float(incident[peak]),
                             "direction": direction, "persistence_samples": count,
                             "persistence_s": float(incident_times[last] - incident_times[first]),
                             "spike": not sustained, "strength": strength, "gap_before": gap_before})
        index += 1
    result["episodes"] = episodes
    return result
