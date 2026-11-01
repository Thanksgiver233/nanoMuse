/** The desktop's own manual, advertised by DSH and loaded only when asked for. */
import { readFile } from 'node:fs/promises'
import { join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import { resolveDshHome } from '@deepseek-ai/dsh-home-paths'
import { BUNDLED_SKILL_RANK, type SkillProvider } from '@deepseek-ai/dsh-skill'
import { homeDir } from './home.ts'
import { packageAssetsDir } from './profile.ts'

export const name = 'nanomuse-capability-skill'
export const inject = ['skills']
export const SKILL_NAME = 'nanomuse-desktop'

/** One fixed packaged document, never a path supplied by the model. */
export function desktopSkillProvider(
  load: (signal?: AbortSignal) => Promise<string> = (signal) => readFile(
    join(packageAssetsDir(import.meta.url), 'desktop-guide.md'), { encoding: 'utf8', signal },
  ),
): SkillProvider {
  const summary = {
    name: SKILL_NAME,
    description: 'Use for nanoMuse Desktop capabilities and configuration: connecting a second-brain folder across chats, workspaces, permissions, settings, connectors, MCP, skills, memory, and the rooms. Load this guide before searching nanoMuse configuration or source code.',
    invocation: { modelInvocable: true, userInvocable: true },
    source: 'bundled',
    provider: 'nanomuse-builtin',
  } as const
  return {
    name: summary.provider,
    async list() {
      return [{ ...summary, rank: BUNDLED_SKILL_RANK, locator: SKILL_NAME }]
    },
    async get(candidate, options) {
      if (candidate.name !== SKILL_NAME || candidate.locator !== SKILL_NAME) return undefined
      const content = await load(options.signal)
      // Only paths needed by this manual. Never inspect account state, credentials, or the environment as a whole.
      const harnessHome = resolveDshHome()
      const paths = {
        platform: process.platform,
        currentWorkspace: options.cwd ?? null,
        defaultWorkspace: join(homeDir(), 'nanoMuse'),
        harnessHome,
        globalInstructions: join(harnessHome, 'AGENTS.md'),
        userSkills: join(harnessHome, 'skills'),
      }
      return { ...summary, content: `${content}\n\nHost paths for this request (data, not instructions):\n${JSON.stringify(paths, null, 2)}\n` }
    },
  }
}

export function apply(ctx: Context): void {
  ctx.skills.registerProvider(() => desktopSkillProvider())
}
