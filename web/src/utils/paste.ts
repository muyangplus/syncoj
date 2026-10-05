/**
 * 粘贴表格的解析工具。
 *
 * 教师粘名单/题目清单是主要录入方式，三个地方都要做同一件事 —— 与其抄三份，
 * 不如放在这里一份。**分隔符的尝试顺序是有讲究的**：
 *
 *   制表符 → 英文逗号 → 中文逗号 → 连续空白
 *
 * Excel 复制出来是制表符；手敲的多半用逗号。空白放在最后，因为"张 三"这种
 * 带空格的名字会被空白切坏 —— 而用空白分隔时，教师本来就只能写没有空格的字段。
 */

/** 按一行文本切列。 */
export function splitColumns(line: string): string[] {
  if (line.includes('\t')) return line.split('\t')
  if (line.includes(',')) return line.split(',')
  if (line.includes('，')) return line.split('，')
  return line.split(/\s+/)
}

/** 一行原始文本切好并去掉首尾空白后的列。 */
export function splitColumnsTrimmed(line: string): string[] {
  return splitColumns(line).map((cell) => cell.trim())
}

export interface PasteLine {
  /** 在原文里的行号（从 1 开始），报错时指给教师看 */
  line: number
  columns: string[]
}

/**
 * 把整段文本切成"有内容的行"。
 *
 * 空行与 `#` 开头的行会被跳过 —— 教师习惯在粘之前加几行说明。
 */
export function parsePasteLines(raw: string): PasteLine[] {
  const lines: PasteLine[] = []
  raw.split(/\r?\n/).forEach((text, index) => {
    const trimmed = text.trim()
    if (!trimmed || trimmed.startsWith('#')) return
    lines.push({ line: index + 1, columns: splitColumnsTrimmed(trimmed) })
  })
  return lines
}
