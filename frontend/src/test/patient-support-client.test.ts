import { expect, it } from 'vitest';
import { supportCase, supportPage } from '../patient/support';

const id='00000000-0000-0000-0000-000000000001';
const row={id,category:'login_help',status:'open',resolution_code:null,created_at:'2026-11-03T00:00:00Z',updated_at:'2026-11-03T00:00:00Z'};
it('copies only public structured case fields and queue UUIDs',()=>{
  const own=supportCase({...row,private_text:'<script>not returned</script>',requester_account_id:id});
  expect(Object.keys(own).sort()).toEqual(Object.keys(row).sort());
  expect(supportPage({items:[row],limit:20,offset:10000})).toEqual([row]);
  expect(supportCase({...row,requester_account_id:id,assigned_account_id:null},true).assigned_account_id).toBeNull();
});
it('rejects invalid fields, state combinations, dates, pages and queue account identifiers',()=>{
  const invalid:unknown[]=[null,{},...Object.keys(row).map(key=>({...row,[key]:123})),
    {...row,category:'<img src=x onerror=alert(1)>'},{...row,status:'<script>x</script>'},
    {...row,resolution_code:'not_an_outcome'},{...row,created_at:'not a timestamp'},
    {...row,status:'resolved'},{...row,resolution_code:'guidance_provided'},
    {...row,requester_account_id:id,assigned_account_id:id},
    {...row,requester_account_id:'upstream-sub',assigned_account_id:null},
    {...row,status:'in_progress',requester_account_id:id,assigned_account_id:null}];
  for(const value of invalid) expect(()=>supportCase(value,true)).toThrow('服务暂不可用，请重试。');
  for(const value of [null,{}, {items:[],limit:100,offset:0},{items:[],limit:20,offset:10001},
    {items:[],limit:20,offset:-1},{items:[],limit:20,offset:0.5},{items:Array(21).fill(row),limit:20,offset:0}])
    expect(()=>supportPage(value)).toThrow('服务暂不可用，请重试。');
});
