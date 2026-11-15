import type { ComponentType } from 'react'
import type { RouteId } from '../router/appRoutes'
import { EcgListPage } from './EcgListPage'
import { EcgPage } from './EcgPage'

/** Issue 199: the ECG list and the calibrated viewer. */
export const ECG_PAGES: Partial<Record<RouteId, ComponentType>> = { 'ecg-list': EcgListPage, ecg: EcgPage }
