"use client";

import type { DocumentContent } from "@/lib/types";

export function DocumentPages({ document }: { document: DocumentContent }) {
  return <div className="document-pages">
    <p className={document.extraction_complete ? "muted" : "error-text"}>
      {document.extraction_complete ? "正文抽取完成" : "部分内容未能提取"}
    </p>
    {document.pages?.length ? document.pages.map((page, i) => <details key={i}>
      <summary>{page.locator} · {({
        extracted: "原生文本",
        ocr_review: "OCR 文本",
        needs_review: "识别未完成",
        empty: "空白页",
      } as Record<string, string>)[page.status] ?? page.status}</summary>
      <pre>{page.text || "无可用正文"}</pre>
      {page.tables.map(table => <div className="review-table-scroll" key={table.table_id}>
        <small>表 {table.table_id} · 位置 {table.bbox?.join(", ") ?? "坐标未提供"}</small>
        <table><tbody>{table.cells.map((row, r) => <tr key={r}>
          {row.map((cell, c) => <td key={c}>{cell}</td>)}
        </tr>)}</tbody></table>
      </div>)}
    </details>) : <pre>{document.content}</pre>}
  </div>;
}
