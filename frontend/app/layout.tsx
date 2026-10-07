import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'OpenTalk · Voice demo',
  description: 'A simple voice conversation demo with session history.',
};
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
