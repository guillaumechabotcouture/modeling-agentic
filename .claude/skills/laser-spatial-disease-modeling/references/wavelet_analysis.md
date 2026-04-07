# Wavelet Analysis for Spatial Epidemic Phase Differences

## Overview

Wavelet analysis is used to detect traveling wave patterns in epidemic data by computing phase differences between a reference city (e.g., London) and all other locations. The continuous wavelet transform (CWT) with a complex Morlet wavelet decomposes time series into time-frequency space, enabling detection of phase relationships at specific periodicities (annual and biennial cycles).

## Dependencies

```python
import numpy as np
import pandas as pd
import pywt  # PyWavelets
from scipy.stats import linregress
```

## Helper Functions

### Data Padding

Pad time series to the next power of 2 for efficient CWT computation:

```python
def pad_data(x):
    nx = len(x)
    nx2 = (2 ** np.ceil(np.log(nx) / np.log(2))).astype(int)
    x2 = np.zeros(nx2, dtype=x.dtype)
    offset = (nx2 - nx) // 2
    x2[offset:(offset + nx)] = x
    return x2
```

### Log Transform

Standardize case counts for wavelet analysis:

```python
def log_transform(x):
    x = np.log(x + 1)
    m = np.mean(x)
    s = np.std(x)
    x = (x - m) / s
    return x
```

### Compute Wavelet Transform

```python
def calc_Ws(cases):
    log_cases = pad_data(log_transform(cases))
    wavelet = pywt.ContinuousWavelet('cmor2-1')
    dt = 1  # 1 week
    widths = np.logspace(np.log10(1), np.log10(7 * 52), int(7 * 52))
    cwt, frequencies = pywt.cwt(log_cases, widths, wavelet, dt)

    # Trim padded edges
    nt = len(cases)
    offset = (cwt.shape[1] - nt) // 2
    cwt = cwt[:, offset:offset + nt]

    return cwt, frequencies
```

**Wavelet parameters:**
- `cmor2-1`: Complex Morlet wavelet with bandwidth=2, center frequency=1
- `widths`: Logarithmically spaced scales from 1 week to 7 years
- Output `frequencies`: Corresponding frequencies in cycles/week

---

## Phase Difference Computation

Compute cross-wavelet phase differences between a reference city and all other locations:

```python
def wavelet_phase_diff(data, distances, ref_cwt):
    """
    Parameters:
        data: DataFrame with 'cases' column or 2D ndarray (weeks x patches)
        distances: 1D array of distances from reference city (dataset-specific units;
                   for England & Wales this is typically a normalized index, not km)
        ref_cwt: CWT of reference city time series
        max_distance: Maximum distance threshold for analysis (default: 30).
                      Cities beyond this distance are skipped.

    Returns:
        x: distances array
        y: phase differences at biennial period (~2 year)
        y2: phase differences at annual period (~1 year)

    Note:
        The distance threshold (default 30) is in the same units as the distances
        array. For the England & Wales dataset, distances are often provided as a
        normalized distance index rather than raw km. Adjust max_distance based on
        your distance metric.
    """
    if isinstance(data, pd.DataFrame):
        x = np.zeros(len(data))
        y = np.zeros(len(data))
        y2 = np.zeros(len(data))
        for i, row in data.iterrows():
            if distances[i] > 30:
                continue
            cwt, frequencies = calc_Ws(row["cases"].flatten())
            diff = ref_cwt * np.conj(cwt)

            # Biennial band: periods between 1.5 and 3 years
            ind = np.where(np.logical_and(
                frequencies < 1 / (1.5 * 52),
                frequencies > 1 / (3 * 52)
            ))
            diff1 = diff[ind[0], :]
            x[i] = distances[i]
            y[i] = np.angle(np.mean(diff1))

            # Annual band: periods between 0.75 and 1.25 years
            ind2 = np.where(np.logical_and(
                frequencies < 1 / (0.75 * 52),
                frequencies > 1 / (1.25 * 52)
            ))
            diff2 = diff[ind2[0], :]
            y2[i] = np.angle(np.mean(diff2))

    elif isinstance(data, np.ndarray):
        x = np.zeros(data.shape[1])
        y = np.zeros(data.shape[1])
        y2 = np.zeros(data.shape[1])
        for i in range(data.shape[1]):
            if distances[i] > 30:
                continue
            cwt, frequencies = calc_Ws(data[:, i].flatten())
            diff = ref_cwt * np.conj(cwt)
            ind = np.where(np.logical_and(
                frequencies < 1 / (1.5 * 52),
                frequencies > 1 / (3 * 52)
            ))
            diff1 = diff[ind[0], :]
            x[i] = distances[i]
            y[i] = np.angle(np.mean(diff1))
            ind2 = np.where(np.logical_and(
                frequencies < 1 / (0.75 * 52),
                frequencies > 1 / (1.25 * 52)
            ))
            diff2 = diff[ind2[0], :]
            y2[i] = np.angle(np.mean(diff2))

    return x, y, y2
```

---

## Phase Similarity Scoring

Compare observed and simulated phase difference patterns.

> **Note:** An importable version of `phase_similarity` is available in `scripts/calibration_metrics.py`. The code below is for reference.

```python
def phase_similarity(y_obs, y_sim, mask):
    """Sum of squared differences in phase (degrees) at valid cities."""
    return np.sum(
        ((-180 / np.pi) * y_obs[mask] - (-180 / np.pi) * y_sim[mask]) ** 2
    )

# Usage:
mask = (x_data > 0) & (y_sim != 0) & (y_data != 0)
score = phase_similarity(y_data, y_sim, mask)
```

---

## Complete Workflow Example

```python
# 1. Compute observed phase differences from London
london_idx = EWdata[EWdata["name"].str.contains("London")].index[0]
london_cases = EWdata.loc[london_idx, "cases"]
ref_cwt, frequencies = calc_Ws(np.array(london_cases.flatten()))
distances_from_london = distances[london_idx, :]

x_data, y_data, y2_data = wavelet_phase_diff(
    EWdata, distances_from_london, ref_cwt
)

# 2. For each simulation, compute phase differences
for sim_idx in range(nsims):
    cases = weekly_incidence[sim_idx, :, :]
    london_sim = cases[:, london_idx].flatten()
    ref_cwt_sim, _ = calc_Ws(np.array(london_sim[520:]).flatten())
    x_sim, y_sim, y2_sim = wavelet_phase_diff(
        cases[520:, :], distances_from_london, ref_cwt_sim
    )

# 3. Plot: phase difference vs. distance from London
plt.plot(x_data, -y_data * 180 / np.pi, 'o')
plt.xlim(5, 30)
plt.ylim(-90, 0)
plt.xlabel("Distance from London (dataset units)")
plt.ylabel("Phase difference (degrees)")
plt.title("Traveling Wave Phase Differences")
```

---

## Interpretation

- **Negative phase differences** indicate the reference city (London) leads other cities in epidemic timing
- **Linear relationship** between distance and phase lag indicates a traveling wave
- **Biennial band** (1.5-3 year period) captures the dominant epidemic cycle in measles
- **Annual band** (0.75-1.25 year period) captures yearly transmission patterns
- Cities within the distance threshold (default 30, in dataset-specific units) are analyzed; beyond that, spatial coupling is too weak for meaningful phase estimation
- Burn-in period of 520 weeks (10 years) is skipped before wavelet analysis
