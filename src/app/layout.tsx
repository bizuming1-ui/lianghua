import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "生物科技ETF 量化终端",
  description: "IBB / XBI / 159502 秒级行情、模拟交易与量化信号",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="overflow-hidden bg-black text-slate-200 antialiased">{children}</body>
    </html>
  );
}
