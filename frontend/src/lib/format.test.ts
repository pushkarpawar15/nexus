import { describe, expect, it } from 'vitest';
import { approvalLabel, laneFor, label, relativeDate } from './format';
import type { Task } from '../domain/types';

const task = (status: Task['status'], mode: Task['mode'] = null) =>
  ({ status, mode }) as Task;
const partialTask = (value: Partial<Task>) => value as unknown as Task;

describe('workflow presentation', () => {
  it('maps states and execution modes into the ordered board lanes', () => {
    expect(laneFor(task('COMPLETED'))).toBe('Shipped');
    expect(laneFor(task('PR_CREATED'))).toBe('PR Review');
    expect(laneFor(task('BLOCKED'))).toBe('Needs Attention');
    expect(laneFor(task('SECURITY_REVIEW'))).toBe('Approval');
    expect(laneFor(task('VALIDATING'))).toBe('Validation');
    expect(laneFor(task('READY', 'HUMAN'))).toBe('Human Queue');
    expect(laneFor(task('READY', 'HYBRID'))).toBe('Hybrid Queue');
    expect(laneFor(task('READY', 'AI'))).toBe('AI Queue');
    expect(laneFor(task('BACKLOG'))).toBe('Backlog');
  });

  it('formats labels and invalid dates safely', () => {
    expect(label('SECURITY_REVIEW')).toBe('Security Review');
    expect(relativeDate('not-a-date')).toBe('—');
  });

  it('derives the approval snapshot from task state and transition evidence', () => {
    expect(
      approvalLabel(
        partialTask({
          approval_required: true,
          status: 'AWAITING_APPROVAL',
          transition_history: [],
        }),
      ),
    ).toBe('Awaiting decision');
    expect(
      approvalLabel(
        partialTask({
          approval_required: false,
          status: 'APPROVED',
          transition_history: [],
        }),
      ),
    ).toBe('Approved');
  });
});
