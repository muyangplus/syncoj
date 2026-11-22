// 由 scripts/collect_licenses.py 生成，请勿手改。
export interface RuntimeCredit {
  component: string
  name: string
  version: string
  license: string
}

export const RUNTIME_CREDITS: readonly RuntimeCredit[] = [
  { component: '前端', name: '@element-plus/icons-vue', version: '2.3.2', license: 'MIT' },
  { component: '前端', name: 'dompurify', version: '3.4.16', license: '(MPL-2.0 OR Apache-2.0)' },
  { component: '前端', name: 'element-plus', version: '2.14.7', license: 'MIT' },
  { component: '前端', name: 'marked', version: '18.1.0', license: 'MIT' },
  { component: '前端', name: 'pinia', version: '2.3.1', license: 'MIT' },
  { component: '前端', name: 'vue', version: '3.5.43', license: 'MIT' },
  { component: '前端', name: 'vue-router', version: '4.6.4', license: 'MIT' },
  { component: '服务端', name: 'fastapi', version: '>=0.100,<1.0', license: 'MIT' },
  { component: '服务端', name: 'uvicorn', version: '>=0.23', license: 'BSD-3-Clause' },
  { component: '服务端', name: 'SQLAlchemy', version: '>=2.0,<3.0', license: 'MIT' },
  { component: '服务端', name: 'pydantic', version: '>=2.0,<3.0', license: 'MIT' },
  { component: '服务端', name: 'python-multipart', version: '>=0.0.6', license: 'Apache-2.0' },
  { component: '评测（随仓库分发）', name: 'testlib.h', version: '0.9.51', license: 'MIT' },
]
