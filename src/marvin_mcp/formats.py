"""How to read the table and chart blocks in Marvin's answers.

Marvin writes tables and charts as JSON in fenced ``financial-table`` and
``echarts`` blocks, which the Marvin app renders. An MCP client gets the raw
JSON, and the ``get_format_guide`` tool serves this guide on request rather
than repeating it in every answer.
"""

from __future__ import annotations

FORMAT_GUIDE = """\
# Rendering Marvin's table and chart blocks

Marvin's answers are markdown. Tables and charts arrive as JSON inside fenced code
blocks whose language is `financial-table` or `echarts`. Replace each block with a
rendering the user can read, in place, keeping the text around it. Never change a
number: the blocks hold raw values and only the formatting is yours to do.

## ```financial-table

```json
{
  "title": "Margins compressing as input costs rise",
  "subtitle": "$M, FY23A-FY25E",
  "columns": [
    {"id": "fy23", "label": "FY23", "spanner": "Actuals"},
    {"id": "fy24", "label": "FY24", "spanner": "Actuals"},
    {"id": "fy25", "label": "FY25E", "spanner": "Estimates"}
  ],
  "rows": [
    {"label": "Revenue", "type": "money", "currency": "USD", "unit": "M", "decimals": 0,
     "cells": {"fy23": 1200, "fy24": 1350, "fy25": 1500}},
    {"label": "Gross margin", "type": "percent", "decimals": 1, "indent": 1,
     "cells": {"fy23": 42.3, "fy24": 40.1, "fy25": "NM"}},
    {"label": "Net income", "style": "total", "type": "money", "currency": "USD", "unit": "M",
     "cells": {"fy23": -12, "fy24": 85}}
  ],
  "source": "Company filings, Marvin Labs",
  "notes": ["FY25E reflects consensus estimates"]
}
```

Renders as:

**Margins compressing as input costs rise**
*$M, FY23A-FY25E*

| | Actuals: FY23 | Actuals: FY24 | Estimates: FY25E |
|---|---:|---:|---:|
| Revenue | $1,200M | $1,350M | $1,500M |
| &nbsp;&nbsp;Gross margin | 42.3% | 40.1% | NM |
| **Net income** | **($12M)** | **$85M** | |

Source: Company filings, Marvin Labs. FY25E reflects consensus estimates.

**Layout**
- `title` is the heading, `subtitle` the units line under it; `source` and `notes` go
  under the table.
- `columns` are the headers, left to right. The first markdown column holds each
  row's `label`.
- `rows[].cells` maps a column `id` to the value. A missing key or `null` is an empty
  cell.
- A `spanner` groups adjacent columns under one header ("Actuals", "Estimates").
  Markdown has one header row, so prefix it: "Actuals: FY23".

**Row `style`**
- `normal`: the default.
- `subtotal` and `total`: bold the whole row.
- `header`: a section label inside the table. Bold the label and leave the cells
  empty.
- `spacer`: an empty separator row.
- `indent` (0-4): indent the label, for example two `&nbsp;` per level.

**Number formatting**

A cell takes each format field from its column if the column sets it, else from its
row, else the default. The fields are `type`, `decimals`, `currency`, `unit` and
`numberFormat`. The default type is `number`.

| `type` | Value | Shows as |
|---|---|---|
| `money` | `1200`, currency USD, unit M | `$1,200M`; negatives in parentheses: `($12M)` |
| `number` | `1500000` | `1,500,000` |
| `percent` | `42.3` (already a percentage, not 0.423) | `42.3%` |
| `multiple` | `12.3` | `12.3x` |
| `bps` | `110` | `110bps` |
| `text`, `text_nowrap` | any string | as is |

- `unit` is a magnitude suffix that the value is already in: `K`, `M`, `B`, `T`, or
  `x1` for none. Do not scale the value.
- `currency` is an ISO code. Use its symbol (USD `$`, EUR `€`, GBP `£`, JPY `¥`) or
  the code for others ("KRW 1,200T").
- `decimals` is the number of decimal places. Without it, use 0 for money and
  numbers, and 1 for percent, multiple and bps.
- `numberFormat` is an Excel-style format string, such as `#,##0;(#,##0)`. Follow it
  when present.
- The strings `NM` (not meaningful), `NA` (not available) and `--` (not applicable)
  are shown as they are.
- Right-align numeric columns and left-align text, unless `align` says otherwise.
- Ignore `minWidth` and `maxWidth`.

## ```echarts

An [Apache ECharts](https://echarts.apache.org) option object, with one extra key.

```json
{
  "title": {"text": "Revenue growth re-accelerates", "subtext": "$M, 1Q24-4Q24"},
  "sourceAttribution": "Company filings, Marvin Labs",
  "tooltip": {"trigger": "axis"},
  "xAxis": {"type": "category", "data": ["1Q24", "2Q24", "3Q24", "4Q24"]},
  "yAxis": {"type": "value", "name": "$M"},
  "series": [
    {"name": "Revenue", "type": "bar", "data": [310, 325, 340, 375]},
    {"name": "Gross profit", "type": "line", "data": [130, 133, 141, 160]}
  ]
}
```

**If you can draw charts**, for example in an HTML artifact or with a plotting
library:
- The object is a valid ECharts option once you remove `sourceAttribution`.
- Pass it to `echarts.init(el).setOption(option)` and add your own colours.
- Show `sourceAttribution` as a caption under the chart.

**Otherwise, show the data as a markdown table.** Give it the chart's `title.text`
as a bold heading, `title.subtext` as the units line, and `sourceAttribution` as
the source.
- Category axis (`xAxis.type` = `category`): one row per `xAxis.data` entry, one
  column per series, headed by the series `name`. `series[i].data[j]` belongs to
  `xAxis.data[j]`.
- Horizontal bars put the categories on `yAxis.data` instead; read them the same
  way.
- `pie`: one row per `{"name", "value"}` item in the series `data`.
- Waterfall (stacked bars): one series is an invisible base that only lifts the
  bars. Leave it out and show the visible series.
- `markLine` / `markArea` mark where actuals end and estimates begin. Note the
  boundary, for example by putting "(E)" after estimate periods.
- The numbers are raw: take the units from `title.subtext` or `yAxis.name`.
- A line chart's one-line takeaway is usually in the text around it, so keep that
  text.
"""

