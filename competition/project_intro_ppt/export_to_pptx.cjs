const path = require('node:path');
const fs = require('node:fs');
const pptxgen = require('pptxgenjs');

const root = path.resolve('competition/project_intro_ppt');
const imageDir = path.join(root, 'pptx_slides');
const output = path.join(root, 'GlobalPool-AI-项目介绍.pptx');

const pptx = new pptxgen();
pptx.layout = 'LAYOUT_WIDE';
pptx.author = 'GlobalPool AI';
pptx.company = 'GlobalPool AI';
pptx.subject = '实时海外仓经营沙盘项目介绍';
pptx.title = 'GlobalPool AI 实时经营沙盘 · 项目介绍';
pptx.lang = 'zh-CN';
pptx.theme = {
  headFontFace: 'Helvetica Neue',
  bodyFontFace: 'Helvetica Neue',
  lang: 'zh-CN',
};
pptx.defineLayout({ name: 'GLOBALPOOL_WIDE', width: 13.333333, height: 7.5 });
pptx.layout = 'GLOBALPOOL_WIDE';
pptx.margin = 0;
pptx.subject = '实时经营沙盘 + 预测补货 + 库存池分仓 + 仿真对比';
pptx.creationDate = new Date();

for (let i = 1; i <= 10; i += 1) {
  const slide = pptx.addSlide();
  slide.background = { color: 'FFFFFF' };
  const imagePath = path.join(imageDir, `slide-${String(i).padStart(2, '0')}.png`);
  if (!fs.existsSync(imagePath)) throw new Error(`Missing slide image: ${imagePath}`);
  slide.addImage({ path: imagePath, x: 0, y: 0, w: 13.333333, h: 7.5, altText: `GlobalPool AI project intro slide ${i}` });
}

pptx.writeFile({ fileName: output }).then(() => console.log(output));
