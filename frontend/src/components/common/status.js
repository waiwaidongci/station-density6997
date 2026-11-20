import {runStates,reviewStates,cardStates} from '../../constants/enums.js';import {esc} from '../../utils/format.js';
export function status(state){const label=runStates[state]||reviewStates[state]||cardStates[state]||state;return '<span class="status '+esc(state)+'">'+esc(label)+'</span>'}
