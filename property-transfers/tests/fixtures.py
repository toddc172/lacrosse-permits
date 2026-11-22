"""Synthetic report and in-memory private storage for offline tests."""
import copy
import csv
import gzip
import hashlib
import io
import transfer_data as data

def source(month='2026-09', price='$100,000.00', document='123', extra=None):
    row = dict.fromkeys(data.HEADERS, '')
    row.update({'County':'La Crosse','Document Number':document,'Recorded Date':month[5:]+'-02-'+month[:4],
        'Conveyance Date':month[5:]+'-01-'+month[:4], 'Municipality':'La Crosse, City of',
        'Parcel Number':'\t001-00001-000','Physical Address':'123 MAIN ST','Conveyance Type':'Sale',
        'Sale Price':price,'Estimated Value':'$0.00','Grantor Name':'SOURCE NAME','Grantee Name':'OTHER NAME'})
    if extra: row.update(extra)
    f=io.StringIO(newline='');writer=csv.DictWriter(f,fieldnames=data.HEADERS);writer.writeheader();writer.writerow(row)
    return gzip.compress(f.getvalue().encode(),mtime=0)

class API:
    def __init__(self): self.values={};self.uploads=[]
    def get_json(self,k): return copy.deepcopy(self.values.get(k))
    def get_value(self,k,**kw): return self.values[k]
    def put_json(self,k,v): self.values[k]=copy.deepcopy(v)
    def delete_value(self,k): self.values.pop(k,None);self.uploads=[u for u in self.uploads if u['name']!=k]
    def list_uploads(self): return copy.deepcopy(self.uploads)
    def add(self,month,raw):
        name='RETRHistoricalReport'+month.replace('-','')+'.csv';digest=hashlib.sha256(raw).hexdigest()
        ident=hashlib.sha256((name+'\n'+digest).encode()).hexdigest()
        meta={'schema_version':1,'id':ident,'name':name,'bytes':len(raw),'sha256':digest,'uploaded_at':'2026-11-01T00:00:00Z','media_type':'text/csv'}
        self.values['uploads/'+ident]=raw;self.uploads.append({'name':'uploads/'+ident,'metadata':meta});return ident
