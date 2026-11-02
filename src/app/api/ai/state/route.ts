export const dynamic = "force-dynamic";

// AI 推理(vLLM 双 T4)只在 Kaggle 版后端运行;本站(Next.js)负责浏览器模拟器与代码分发。
export async function GET() {
  return Response.json({
    available: false,
    reason: "AI 推理决策运行在 Kaggle(T4×2)后端:在『复制代码界面』复制 Cell 0 与 Cell 1 到 Kaggle 运行后,打开它输出的网址即可使用。",
  });
}
