/**
 * 把公告正文按 Markdown 渲染成 HTML。
 *
 * 为什么需要一个库：公告是教师手写的一段话，`##`、`-`、`**` 这类写法他一定会用，
 * 而按纯文本原样显示出来就是一堆符号。手写一个"够用的 Markdown"最后一定会漏掉表格、
 * 嵌套列表、转义这些边角，而那时没人会去补。
 *
 * **必须消毒**：这段 HTML 会被 `v-html` 插进页面。教师账号是受信的，但"受信的输入"
 * 不等于"安全的 HTML" —— 一份从别处粘进来、带 `<img onerror=…>` 的公告，会把考场
 * 机器上的这一页变成任何人的脚本宿主（公开端口上虽然没有任何凭据，但页面本身会被
 * 改写、会被挂钓鱼的假公告）。所以渲染完一律过一遍 DOMPurify，只留白名单里的标签。
 */
import DOMPurify from 'dompurify'
import { marked } from 'marked'

//: 外链在新标签页打开：选手点一下链接不该把这一页（和他的公告）顶掉。
//: 只在**消毒之后**加，且补上 noopener —— 不加的话新标签页能通过 window.opener
//: 反过来改这一页。
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('href')) {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

marked.setOptions({
  // **单个换行也要断行**（GFM 的 behavior，`breaks: true`）。
  //
  // 这条不是风格选择：公告是在一个纯文本框里写的，写的人未必在想 Markdown 语法。
  // 他写下
  //
  //     不许带手机
  //     不许互相交谈
  //
  // 却看到这两行被合成一整句显示出来 —— 那是"渲染错了"，不是"他没按 Markdown 写"。
  // 关掉它（CommonMark 默认）等于把原来 `pre-wrap` 的保证偷偷去掉：老师一行一条地
  // 写，页面上必须一行一条地显示。
  breaks: true,
})

export function renderMarkdown(text: string): string {
  const html = marked.parse(text, { async: false })
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } })
}
