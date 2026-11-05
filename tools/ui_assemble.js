/**
 * 模板拼装（node 侧），与 live/uiparts.py 同一套规则。
 *
 * 为什么要两份实现：面板由 Python 拼装发给浏览器，冒烟测试在 node 里跑
 * （tools/smoke_dashboard.js 用假 DOM 执行页面脚本），中间没有浏览器也没有
 * 服务端。两份实现必须给出**同一个字符串**，否则冒烟绿了不等于页面对。
 * tests/test_ui_parts.py 里有一条断言直接比对两边输出，别只改一边。
 *
 * 规则（与 uiparts.py 一字不差）：
 *   index.html 里 `<!--@include 相对路径-->` 独占一行，整行替换为该文件的
 *   全部内容；不重排缩进、不补空行。
 */
const fs = require('fs');
const path = require('path');

const INCLUDE = /^<!--@include\s+([^>\s]+)\s*-->\s*(?:\r?\n)?$/;

function assemble(dir) {
  const index = path.join(dir, 'index.html');
  const text = fs.readFileSync(index, 'utf8');
  // 保留行尾：不然 CRLF/LF 的差异会让两边拼装结果对不上。
  const lines = text.match(/[^\n]*\n|[^\n]+$/g) || [];
  const out = [];
  lines.forEach((line, i) => {
    const m = INCLUDE.exec(line);
    if (!m) {
      out.push(line);
      return;
    }
    const part = path.join(dir, m[1]);
    if (!fs.existsSync(part)) {
      throw new Error(`tools/ui/index.html 第 ${i + 1} 行的 include 找不到文件：${m[1]}`);
    }
    out.push(fs.readFileSync(part, 'utf8'));
  });
  return out.join('');
}

module.exports = { assemble };
