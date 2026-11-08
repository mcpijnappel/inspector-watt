import { mount } from 'svelte';
import App from './App.svelte';
import './app.css';
import { theme } from './lib/theme.svelte';

theme.apply(); // before mounting, so charts read the right colours

mount(App, { target: document.getElementById('app')! });
