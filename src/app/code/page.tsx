import CodeViewer from "@/components/CodeViewer";
import { buildCodePack } from "@/lib/codePack";

export const dynamic = "force-dynamic";

export const metadata = { title: "复制代码 · 生物科技ETF量化终端" };

export default async function CodePage() {
  const pack = await buildCodePack();
  return <CodeViewer pack={pack} />;
}
