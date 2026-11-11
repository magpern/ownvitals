import { expect, test } from '@playwright/test'

// The first-visit cookie notice (#420): information only, shown once, remembered.
test.use({ storageState: { cookies: [], origins: [] } })

test('a fresh visitor sees the notice, "Got it" closes it and a reload does not bring it back', async ({
  page,
}) => {
  await page.goto('/signin')
  const dialog = page.getByRole('dialog', { name: 'Cookies and local storage' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('link', { name: 'Read the cookie statement' })).toHaveAttribute('href', '/cookies')
  await dialog.getByRole('button', { name: 'Got it' }).click()
  await expect(dialog).toBeHidden()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible()
  await expect(dialog).toBeHidden()
})
