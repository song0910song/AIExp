# DIALux Luminaire Finder API 文档

## 概述

DIALux Luminaire Finder（灯具搜索器）是 DIAL GmbH 开发的在线灯具搜索引擎，为规划师、建筑师和室内设计师提供灯具产品搜索服务。

- **官网**: https://luminaires.dialux.com/
- **数据库规模**: 约 258 万+ 灯具产品
- **支持语言**: de, en, en-US, es, fr, it, ru, zh

---

## 基础 URL

```
https://luminaires.dialux.com
```

---

## 1. 搜索 API

### 端点

```
GET /{lang}/{skip}/{count}/search/query/{encodedFilterState}?ft={query}
```

### 参数说明

| 参数 | 类型 | 说明 |
|------|------|------|
| `lang` | string | 语言代码 (`zh`, `en`, `de`, `fr`, `it`, `es`, `ru`, `en-US`) |
| `skip` | int | 分页偏移量 (0, 50, 100, ...) |
| `count` | int | 每页结果数 (推荐 50) |
| `encodedFilterState` | string | 菜单筛选状态；无菜单筛选时使用当前基线值 `a231` |

### 可选查询参数 (通过 `?` 附加)

| 参数 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `ft` | string | 全文搜索关键词 (URL 编码)，支持产品型号、品牌名、描述等 | `?ft=ceiling+led` |
| `bf` | string | 品牌筛选 (Base64 编码的 brandId，多个用分隔符连接) | `?bf=2K-0G6GwTreIgzse6jP-Zg` |

### 示例

```bash
# 中文全文搜索 "a231"
GET https://luminaires.dialux.com/zh/0/50/search/query/a231?ft=a231

# 英文搜索 "ceiling led"，第二页
GET https://luminaires.dialux.com/en/50/50/search/query/a231?ft=ceiling%20led

# 使用 ft 参数 + 品牌筛选
GET https://luminaires.dialux.com/en/0/50/search/query/{encodedBits}?ft=ceiling&bf=2K-0G6GwTreIgzse6jP-Zg
```

> 2026-07-27 实测：将普通关键词直接放入路径（例如 `/query/downlight`）会返回 HTTP 500；`/query/a231?ft=downlight` 返回 JSON 结果。因此客户端应把全文关键词放入 `ft`，不能把 `query` 路径段当作普通关键词。

### 响应格式

```json
{
  "hitsText": "50 results, out of 2,582,932 total",
  "maxResultSize": 2582932,
  "isRandom": true,
  "resultSize": 50,
  "result": [
    {
      "luminaireId": "vjI31dOBQ0uWeT6vyXb_0g",
      "articleName": "EBRPL-R2x115/30ND-SM-500M840",
      "brandId": "zDPPA4PqRdWlu4iSWX3Ggg",
      "brandName": "RIDI",
      "mosaicImage": "/files/7VD6iax5QyusOS_nYUI-Hg.jpeg",
      "imageTriplet": "/files/ugoJhv9JRDSHXgJdnXwu6g.png",
      "summaryLine": "Ceiling mounted · LED",
      "technicalSummaryLine": "System power: 68 W · 1197mm x 184mm x 74mm · IP20",
      "toDetails": "/en/article/vjI31dOBQ0uWeT6vyXb_0g",
      "hasUld": true,
      "hasPhotometryDownload": true
    }
  ],
  "brands": [
    {
      "id": "zDPPA4PqRdWlu4iSWX3Ggg",
      "img": "brands-01608908765",
      "name": "RIDI"
    }
  ]
}
```

### 响应字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `hitsText` | string | 结果数量描述文本 |
| `maxResultSize` | int | 匹配的总结果数 |
| `isRandom` | bool | 是否随机搜索（无筛选条件时返回随机结果） |
| `resultSize` | int | 本次返回的结果数 |
| `result` | array | 灯具结果列表 |
| `result[].luminaireId` | string | 灯具唯一标识 |
| `result[].articleName` | string | 产品名称/型号 |
| `result[].brandId` | string | 品牌唯一标识 |
| `result[].brandName` | string | 品牌名称 |
| `result[].mosaicImage` | string | 产品预览图 URL（相对路径） |
| `result[].imageTriplet` | string | 配光曲线图 URL（相对路径） |
| `result[].summaryLine` | string | 简短描述 |
| `result[].technicalSummaryLine` | string | 技术参数摘要 |
| `result[].toDetails` | string | 产品详情页 URL（相对路径） |
| `result[].hasUld` | bool | 是否支持 DIALux 直接导入 |
| `result[].hasPhotometryDownload` | bool | 是否可下载配光文件 |
| `brands` | array | 匹配到的品牌列表 |
| `nonRandom` 时附加字段 | | |
| `buttonTexts` | object | 分页按钮文本 |
| `selectedBrands` | array | 已选品牌 |

---

## 2. 搜索建议/自动补全 API

### 端点

```
GET /{lang}/suggest/{query}
```

### 参数说明

| 参数 | 类型 | 说明 |
|------|------|------|
| `lang` | string | 语言代码 |
| `query` | string | 部分搜索词 |

### 示例

```bash
GET https://luminaires.dialux.com/en/suggest/ceiling
```

### 响应格式

```json
[
  {
    "value": ["Ceiling", "", ""],
    "Count": 3
  },
  {
    "value": ["Ceiling mounted", "", ""],
    "Count": 3
  },
  {
    "value": ["Ceiling recessed", "", ""],
    "Count": 3
  }
]
```

### 响应字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `value` | array[3] | `[名称, 品牌/类型名, 附加信息]` |
| `Count` | int | 匹配数量 |

---

## 3. 产品详情页

### 端点

```
GET /{lang}/article/{luminaireId}
```

### 说明

- 返回 HTML 页面，包含完整产品信息
- 页面内嵌 `articleUid` 和 `brandUid` 变量
- 产品图片通过 `/files/{imageId}.{format}` 获取

### 示例

```bash
GET https://luminaires.dialux.com/en/article/vjI31dOBQ0uWeT6vyXb_0g
```

### 页面嵌入变量

```javascript
var articleUid = 'vjI31dOBQ0uWeT6vyXb_0g';
var brandUid = 'zDPPA4PqRdWlu4iSWX3Ggg';
```

---

## 4. 品牌列表页

### 端点

```
GET /{lang}/brands
```

### 说明

返回 HTML 页面，列出所有品牌。

---

## 5. 图片/文件资源

### 端点

```
GET /files/{fileId}.{format}
```

### 说明

- `fileId`: 文件唯一标识（Base64-like 字符串）
- `format`: 文件格式 (`jpeg`, `png`, `svg` 等)
- 产品图片 URL 从搜索结果中的 `mosaicImage` 和 `imageTriplet` 字段获取

---

## 6. 菜单分类系统 (menuJson)

菜单数据嵌入在 `lumsearchapp.min.js` 中，包含以下 11 个顶级分类：

| 分类名 | 英文名 | 中文名 |
|--------|--------|--------|
| `application` | Application | 应用 |
| `mountmode` | Mounting mode | 安装方式 |
| `adjustability` | Adjustability | 可调节性 |
| `illuminant` | Light source | 光源 |
| `shapeanddimensions` | Shape & dimensions | 形状和尺寸 |
| `lightdistribution` | Light distribution | 光分布 |
| `lightcolor` | Light color | 光色 |
| `des` | Design | 设计 |
| `protection` | Protection class | 防护等级 |
| `electric` | Electrical | 电气参数 |
| `emergencylighting` | Emergency lighting | 应急照明 |

### 高级筛选 URL 格式

当用户选择菜单筛选条件时，URL 格式变为：

```
/{lang}/{skip}/{count}/search/{viewMode}/query/{rleEncodedBits}?{menuParams}
```

- **rleEncodedBits**: RLE 编码的菜单复选框选择位图（超长十六进制字符串）
- **menuParams**: 键值参数，格式 `{menuItemId}={value}` 或 `{menuItemId}={min}_{max}{unit}`
- 多个参数用 `&` 或 `;` 分隔

---

## 7. URL 路径结构汇总

| 用途 | URL 模式 |
|------|----------|
| 搜索页面 | `/{lang}/search/{list|mosaic}/query/{encodedFilterState}?ft={query}` |
| 搜索 JSON API | `/{lang}/{skip}/{count}/search/query/{encodedFilterState}?ft={query}` |
| 搜索建议 | `/{lang}/suggest/{query}` |
| 产品详情 | `/{lang}/article/{luminaireId}` |
| 品牌列表 | `/{lang}/brands` |
| 图片文件 | `/files/{fileId}.{format}` |
| 静态资源 | `/assets/...` |

---

## 8. 使用示例

### curl 示例

```bash
# 搜索灯具
curl -H "Accept: application/json" \
  "https://luminaires.dialux.com/zh/0/50/search/query/a231?ft=LED%20天花板"

# 获取搜索建议
curl -H "Accept: application/json" \
  "https://luminaires.dialux.com/zh/suggest/天花"

# 获取英文结果第二页
curl -H "Accept: application/json" \
  "https://luminaires.dialux.com/en/50/50/search/query/a231?ft=philips"
```

### Python 示例

```python
import requests

# 搜索
resp = requests.get(
    "https://luminaires.dialux.com/zh/0/50/search/query/a231",
    params={"ft": "4000K"},
    headers={"Accept": "application/json"}
)
data = resp.json()
for item in data["result"]:
    print(f"{item['articleName']} - {item['brandName']}")
    print(f"  {item['technicalSummaryLine']}")
    print(f"  详情: https://luminaires.dialux.com{item['toDetails']}")
```

### JavaScript 示例

```javascript
const response = await fetch(
  "https://luminaires.dialux.com/en/0/50/search/query/a231?ft=ceiling+led",
  { headers: { Accept: "application/json" } }
);
const data = await response.json();
console.log(`Total results: ${data.maxResultSize}`);
data.result.forEach(item => {
  console.log(`${item.articleName} | ${item.brandName}`);
});
```

---

## 9. 注意事项

1. **跨域**: 服务器可能限制跨域请求，建议使用后端代理
2. **速率限制**: 请合理控制请求频率，避免被封
3. **编码**: URL 中的中文或特殊字符需要 URL 编码后传递
4. **分页**: `skip` 和 `count` 参数嵌入在 URL 路径中，非查询参数
5. **图片**: 所有图片 URL 为相对路径，需拼接 `https://luminaires.dialux.com` 前缀
6. **isRandom**: 当 `isRandom=true` 时，无分页按钮，只返回单页随机结果
