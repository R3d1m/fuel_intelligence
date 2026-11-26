import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  resetKey?: string;
}

interface State {
  hasError: boolean;
  message: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: '' };

  static getDerivedStateFromError(error: Error): State {
    return {
      hasError: true,
      message: error.message || 'An unexpected rendering error occurred.',
    };
  }

  componentDidUpdate(prevProps: Props) {
    if (prevProps.resetKey !== this.props.resetKey && this.state.hasError) {
      this.setState({ hasError: false, message: '' });
    }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('UI ErrorBoundary caught:', error, info);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="rounded-lg border border-[#FECDD3] bg-[#FFF1F2] p-5">
          <h2 className="text-sm font-semibold text-[#9F1239]">Unable to display this panel</h2>
          <p className="mt-1 text-xs text-[#881337]">{this.state.message}</p>
          <button
            type="button"
            onClick={() => this.setState({ hasError: false, message: '' })}
            className="mt-3 rounded-md border border-[#FDA4AF] bg-white px-3 py-1.5 text-xs font-medium text-[#9F1239] hover:bg-[#FFF1F2]"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
