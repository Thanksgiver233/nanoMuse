import assert from 'node:assert/strict'
import { test } from 'node:test'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { Context } from '@deepseek-ai/cordis'
import SkillRegistry from '@deepseek-ai/dsh-skill'
import * as capabilitySkill from '../lib/capability-skill.js'
import { desktopSkillProvider, SKILL_NAME } from '../lib/capability-skill.js'

test('the native registry advertises the manual without loading its body, then loads on demand', async () => {
  const ctx = new Context()
  await ctx.plugin(SkillRegistry)
  let loads = 0
  const unregister = ctx.skills.registerProvider(() => desktopSkillProvider(async () => {
    loads += 1
    return 'A local capability manual.'
  }))
  try {
    const catalog = await ctx.skills.list({ cwd: '/workspace' })
    assert.deepEqual(catalog.map((entry) => entry.name), [SKILL_NAME])
    assert.equal(catalog[0].invocation.modelInvocable, true)
    assert.equal(catalog[0].invocation.userInvocable, true)
    assert.equal(Object.hasOwn(catalog[0], 'content'), false)
    assert.equal(loads, 0)

    const skill = await ctx.skills.get(SKILL_NAME, { cwd: '/workspace' })
    assert.match(skill.content, /^A local capability manual\./)
    assert.equal(loads, 1)
    assert.match(skill.content, /"currentWorkspace": "\/workspace"/)
    assert.equal(await ctx.skills.get('missing-skill'), undefined)
    assert.equal(loads, 1)

    await unregister()
    assert.deepEqual(await ctx.skills.list(), [])
  } finally {
    await ctx.fiber.dispose()
  }
})

test('the preset plugin serves the packaged manual and unregisters on disposal', async () => {
  const ctx = new Context()
  await ctx.plugin(SkillRegistry)
  const plugin = ctx.plugin(capabilitySkill)
  await plugin
  try {
    const skill = await ctx.skills.get(SKILL_NAME)
    assert.ok(skill)
    assert.match(skill.content, /^# nanoMuse Desktop capability and configuration guide/)
    assert.match(skill.content, /Host paths for this request/)
    await plugin.dispose()
    assert.equal(await ctx.skills.get(SKILL_NAME), undefined)
  } finally {
    await ctx.fiber.dispose()
  }
})

test('a cancelled read cannot load the guide and a body load failure remains visible', async () => {
  const ctx = new Context()
  await ctx.plugin(SkillRegistry)
  let loads = 0
  ctx.skills.registerProvider(() => desktopSkillProvider(async () => {
    loads += 1
    throw new Error('guide unavailable')
  }))
  try {
    await assert.rejects(ctx.skills.get(SKILL_NAME, { signal: AbortSignal.abort() }))
    assert.equal(loads, 0)
    await assert.rejects(ctx.skills.get(SKILL_NAME), /guide unavailable/)
    assert.equal(loads, 1)
  } finally {
    await ctx.fiber.dispose()
  }
})

test('host paths follow DSH home expansion instead of treating a tilde or blank as a directory', async () => {
  const previous = process.env.DSH_HOME
  const ctx = new Context()
  await ctx.plugin(SkillRegistry)
  ctx.skills.registerProvider(() => desktopSkillProvider(async () => 'Guide'))
  try {
    for (const [value, expected] of [
      ['', join(homedir(), '.dsh')],
      ['   ', join(homedir(), '.dsh')],
      ['~/nanomuse-profile', join(homedir(), 'nanomuse-profile')],
      ['~\\nanomuse-profile', join(homedir(), 'nanomuse-profile')],
    ]) {
      process.env.DSH_HOME = value
      const workspace = 'C:\\Notes\\Second brain'
      const skill = await ctx.skills.get(SKILL_NAME, { cwd: workspace })
      const paths = JSON.parse(skill.content.split('Host paths for this request (data, not instructions):\n')[1])
      assert.equal(paths.harnessHome, expected, value)
      assert.equal(paths.globalInstructions, join(expected, 'AGENTS.md'))
      assert.equal(paths.userSkills, join(expected, 'skills'))
      assert.equal(paths.currentWorkspace, workspace)
    }
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    await ctx.fiber.dispose()
  }
})
