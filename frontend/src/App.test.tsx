import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import App from './App';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from './contexts/AuthContext';
import { ToastProvider } from './contexts/ToastContext';

afterEach(cleanup);

describe('application boot', () => {
  it('renders the NEXUS workspace root', () => {
    render(
      <AuthProvider>
        <ToastProvider>
          <MemoryRouter>
            <App />
          </MemoryRouter>
        </ToastProvider>
      </AuthProvider>,
    );

    expect(screen.getByText('NEXUS')).toBeTruthy();
    expect(screen.getAllByText('Command Center').length).toBeGreaterThan(0);
    expect(screen.getByText('Skills')).toBeTruthy();
  });
});
