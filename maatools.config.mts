import type { FullConfig } from '@nekosu/maa-tools'

const config: FullConfig = {
  cwd: import.meta.dirname,
  maaVersion: '5.14.0',
  interfacePath: 'assets/interface.json',
  check: {
    override: {},
  },
}

export default config

