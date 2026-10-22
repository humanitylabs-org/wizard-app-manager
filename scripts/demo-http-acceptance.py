#!/usr/bin/env python3
"""Synthetic real-HTTP attachment, thread-list and metrics acceptance on demo only."""
import hashlib,json,math,os,pathlib,struct,urllib.error,urllib.request,uuid,zlib
P=pathlib.Path(os.environ.get('WIZARD_ACCEPTANCE_EVIDENCE_DIR','/root/wizard-acceptance-20261019'))
BASE='https://demo.tail6adf1a.ts.net:8796'
def req(path,method='GET',body=None,headers=None):
    r=urllib.request.Request(BASE+path,data=body,method=method,headers=headers or {})
    with urllib.request.urlopen(r,timeout=30) as res:return res.status,res.headers,res.read()
def chunk(name,data):return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data)&0xffffffff)
png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',32,32,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b''.join(b'\x00'+bytes([48,160,200])*32 for _ in range(32))))+chunk(b'IEND',b'')
meta={'client_upload_id':'acceptance-'+uuid.uuid4().hex,'filename':'installer-acceptance.png','mime':'image/png','bytes':len(png),'sha256':hashlib.sha256(png).hexdigest()}
s,h,b=req('/wizard/v1/uploads','POST',json.dumps(meta).encode(),{'Content-Type':'application/json'})
created=json.loads(b); uid=created['upload_id']
s,h,b=req('/wizard/v1/uploads/'+uid,'PATCH',png,{'Content-Type':'application/offset+octet-stream','Upload-Offset':'0'})
assert s==204 and h['Upload-Offset']==str(len(png))
s,h,b=req('/wizard/v1/uploads/'+uid+'/complete','POST',b'{}',{'Content-Type':'application/json'})
file=json.loads(b)['file']
(P/'synthetic-upload-file.json').write_text(json.dumps(file,indent=2))
print('upload complete:',json.dumps(file))
# public file metadata has canonical attachment URLs; fetch the actual returned target.
url=file.get('url') or file.get('download_url') or file.get('content_url') or '/wizard/files/'+file['id']+'/content'
assert url, file
if url.startswith('http'):
    with urllib.request.urlopen(url,timeout=30) as r:download=r.read()
else:s,h,download=req(url)
assert download==png
summary={'upload_id':uid,'bytes':len(png),'sha256':hashlib.sha256(png).hexdigest(),'http_roundtrip_byte_identical':True,'file':file}
for key,path in [('metrics','/api/system/load'),('threads','/wizard/hermes/api/sessions?view=list&window=archive-50')]:
    s,h,b=req(path); value=json.loads(b); (P/(key+'-response.json')).write_bytes(b)
    summary[key]={'http_status':s,'shape':list(value.keys()) if isinstance(value,dict) else 'list'}
    if key=='metrics':
        fields=('cpuLoad','percentUsedMemory','percentUsed','memoryTotal','diskUsed','diskSize')
        assert all(isinstance(value[k],(int,float)) and math.isfinite(value[k]) and value[k]>=0 for k in fields)
        assert 0<value['diskUsed']<=value['diskSize'] and value['memoryTotal']>0
        summary[key]['values']=value
    else:
        assert isinstance(value.get('data'),list)
        summary[key]['thread_count']=len(value['data'])
        summary[key]['payload_bytes']=len(b)
(P/'http-acceptance.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
