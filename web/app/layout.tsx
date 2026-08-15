import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "照明设计工作台",
  description: "规范证据、预计算与真实灯具选型工作台",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>{children}</body>
    </html>
  );
}
