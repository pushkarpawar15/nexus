import { useCallback, useEffect, useState } from 'react';
import { useAuth } from '../contexts/authState';
import { listAllTasks } from '../lib/api';
import type { Task } from '../domain/types';
export function useTasks(pollMs = 0) {
  const { user } = useAuth();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    if (!user) {
      setLoading(false);
      return;
    }
    try {
      const result = await listAllTasks(user);
      setTasks(result);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load tasks.');
    } finally {
      setLoading(false);
    }
  }, [user]);
  useEffect(() => {
    void refresh();
    if (pollMs <= 0) return;
    const timer = window.setInterval(() => void refresh(), pollMs);
    return () => window.clearInterval(timer);
  }, [refresh, pollMs]);
  return { tasks, loading, error, refresh };
}
