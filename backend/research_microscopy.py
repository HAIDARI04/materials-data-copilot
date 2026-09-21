"""Calibrated microscopy arrays, explicit leveling, regions and profiles."""
import re
import struct

import numpy as np
from PIL import Image
from scipy.ndimage import label, map_coordinates
from scipy.signal import fftconvolve


PARK_FORMAT_SOURCE = 'https://svn.code.sf.net/p/gwyddion/code/trunk/gwyddion/modules/file/psia.c'


def park(path):
    with Image.open(path) as image:
        tags = image.tag_v2
        version = tags.get(50433)
        if tags.get(50432) != 0x0E031301 or version not in {0x1000001, 0x1000002}:
            raise ValueError('Quantitative AFM requires a supported Park TIFF, not a rendered preview.')
        header = bytes(tags.get(50435, b''))
        data = bytes(tags.get(50434, b''))
    if len(header) < (580 if version == 0x1000002 else 356):
        raise ValueError('Park quantitative header is truncated.')
    def value(fmt, offset):
        return struct.unpack_from('<'+fmt, header, offset)[0]
    def string(start, size):
        return header[start:start+size].decode('utf-16-le').split('\0', 1)[0]
    nx, ny = value('I',100), value('I',104)
    if not (1 < nx <= 8192 and 1 < ny <= 8192 and nx*ny <= 4_000_000):
        raise ValueError('Unsupported quantitative array dimensions.')
    if value('I', 272) or value('I',276) or value('I',280):
        raise ValueError('Compressed or transformed Park data require a dedicated decoder.')
    dtype = {0:'<i2', 1:'<i4', 2:'<f4'}.get(value('I',348) if version == 0x1000002 else 0)
    if not dtype or len(data) != nx*ny*np.dtype(dtype).itemsize:
        raise ValueError('Park quantitative payload size does not match its header.')
    unit = string(244,16)
    unit = {'µm':'um', 'μm':'um'}.get(unit,unit)
    multiplier = {'um':1000, 'nm':1, 'm':1e9, 'V':1, 'mV':.001}.get(unit)
    if multiplier is None:
        raise ValueError(f'Unrecognized Park height/channel unit: {unit!r}. Calibration requires review.')
    output_unit = 'V' if unit in {'V','mV'} else 'nm'
    gain, scale, offset = value('d',220), value('d',228), value('d',236)
    sx, sy = value('d',140), value('d',148)
    if not np.all(np.isfinite([gain,scale,offset,sx,sy])) or sx <= 0 or sy <= 0 or gain == 0:
        raise ValueError('Invalid Park calibration values.')
    raw = np.frombuffer(data,dtype=dtype).reshape(ny,nx).astype(float)
    # Park rows run bottom-to-top. Retain all direction flags as metadata.
    calibrated = np.flipud((raw*(scale or 1)+offset)*gain*multiplier)
    if not np.all(np.isfinite(calibrated)):
        raise ValueError('Non-finite values in quantitative AFM payload.')
    return calibrated, {'channel':string(4,64), 'unit':output_unit, 'source_unit':unit,
        'scan_width_um':sx, 'scan_height_um':sy, 'pixel_width_um':sx/nx, 'pixel_height_um':sy/ny,
        'width':nx, 'height':ny, 'gain':gain, 'scale':scale, 'offset':offset, 'forward':bool(value('I',128)),
        'scan_up':bool(value('I',132)), 'swap_xy':bool(value('I',136)), 'angle_degrees':value('d',108),
        'instrument_auto_flatten':bool(value('I',92)), 'orientation':'stored rows flipped vertically',
        'format_reference':PARK_FORMAT_SOURCE}


def rectangle(bounds, width, height):
    x0,y0,x1,y1 = bounds
    if any(int(v) != v for v in bounds) or not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError('Region must be integer [left, top, right, bottom] pixel bounds inside the image.')
    return slice(y0,y1),slice(x0,x1)


def statistics(values):
    centered = values-values.mean()
    return {'point_count':int(values.size), 'mean':float(values.mean()), 'minimum':float(values.min()),
            'maximum':float(values.max()), 'sa':float(np.mean(np.abs(centered))),
            'sq':float(np.sqrt(np.mean(centered**2))), 'peak_to_valley':float(np.ptp(values))}


def analyze_afm(path, options, backward=None):
    z, calibration = park(path)
    height,width = z.shape
    mask = np.ones(z.shape, dtype=bool)
    for bounds in options.get('exclude_regions', []):
        mask[rectangle(bounds,width,height)] = False
    if mask.sum() < 4:
        raise ValueError('At least four unmasked pixels are required.')
    result = {'calibration':calibration, 'warnings':[], 'summary':{}, 'profiles':[]}
    if backward:
        back, back_calibration = park(backward)
        if z.shape != back.shape or any(not np.isclose(calibration[k],back_calibration[k])
                for k in ['scan_width_um','scan_height_um','angle_degrees']) or calibration['unit'] != back_calibration['unit']:
            raise ValueError('Forward/backward dimensions, angle and calibration must match.')
        if not calibration['forward'] or back_calibration['forward'] or calibration['channel'] != back_calibration['channel']:
            raise ValueError('Select a forward scan and a backward scan of the same channel.')
        dx,dy = options.get('shift_pixels',[0,0])
        if options.get('auto_register'):
            a,b = z-z.mean(), back-back.mean()
            if not a.std() or not b.std():
                raise ValueError('A constant scan cannot be registered.')
            corr = fftconvolve(a,b[::-1,::-1],mode='full')
            cy,cx = np.unravel_index(np.argmax(corr),corr.shape)
            dy,dx = int(cy-(height-1)),int(cx-(width-1))
            if abs(dx) > width//4 or abs(dy) > height//4:
                raise ValueError('Automatic displacement exceeds one quarter scan. Review alignment manually.')
        if abs(dx)>=width or abs(dy)>=height:
            raise ValueError('Registration shift leaves no overlapping pixels.')
        shifted = np.roll(back,(dy,dx),(0,1))
        if dy>0: mask[:dy,:]=False
        elif dy<0: mask[dy:,:]=False
        if dx>0: mask[:,:dx]=False
        elif dx<0: mask[:,dx:]=False
        z = (z-shifted) / (2 if options.get('lfm_convention','difference')=='half_difference' else 1)
        result['registration'] = {'dx_pixels':dx,'dy_pixels':dy,'convention':options.get('lfm_convention','difference'),
                                  'method':'integer cross-correlation' if options.get('auto_register') else 'manual'}
        result['warnings'].append('Trace/retrace contrast is not a friction coefficient without force and detector calibration.')
    yy,xx = np.indices(z.shape)
    level = options.get('level','plane')
    if level == 'plane':
        design = np.column_stack([np.ones(mask.sum()),xx[mask],yy[mask]])
        coefficients,_,rank,_ = np.linalg.lstsq(design,z[mask],rcond=None)
        if rank < 3:
            raise ValueError('Unmasked pixels do not define a plane.')
        z = z-(coefficients[0]+coefficients[1]*xx+coefficients[2]*yy)
        result['plane_coefficients'] = coefficients.tolist()
    elif level == 'line_median':
        for row in range(height):
            if mask[row].any():
                z[row] -= np.median(z[row,mask[row]])
            else:
                mask[row] = False
    elif level != 'none':
        raise ValueError('Choose none, plane or line_median leveling.')
    region = options.get('region',[0,0,width,height])
    region_mask = np.zeros(z.shape,dtype=bool)
    region_mask[rectangle(region,width,height)] = True
    region_mask &= mask
    if region_mask.sum() < 4:
        raise ValueError('The region contains fewer than four valid pixels.')
    result['summary'] = {**statistics(z[region_mask]),'unit':calibration['unit'],
                         'area_um2':float(region_mask.sum()*calibration['pixel_width_um']*calibration['pixel_height_um'])}
    result['map'] = np.where(mask,z,np.nan).tolist()
    result['map'] = [[v if np.isfinite(v) else None for v in row] for row in result['map']]
    for line in options.get('profiles',[]):
        x0,y0,x1,y1 = line
        if not (0<=x0<width and 0<=x1<width and 0<=y0<height and 0<=y1<height):
            raise ValueError('Profile endpoints must lie within the image.')
        count = max(2,int(np.hypot(x1-x0,y1-y0))+1)
        xs,ys = np.linspace(x0,x1,count),np.linspace(y0,y1,count)
        values = map_coordinates(z,[ys,xs],order=1)
        valid = map_coordinates(mask.astype(float),[ys,xs],order=1) >= .999
        length = np.hypot((x1-x0)*calibration['pixel_width_um'],(y1-y0)*calibration['pixel_height_um'])
        result['profiles'].append({'endpoints':line,'points':[[float(d),float(v) if ok else None]
             for d,v,ok in zip(np.linspace(0,length,count),values,valid)]})
    if options.get('threshold') is not None:
        labels,n = label((z>=options['threshold']) & region_mask)
        counts = np.bincount(labels.ravel())[1:]
        areas = counts*calibration['pixel_width_um']*calibration['pixel_height_um']
        result['features'] = [{'label':i+1,'area_um2':float(a),'equivalent_diameter_um':float(2*np.sqrt(a/np.pi))}
                              for i,a in enumerate(areas) if counts[i]>=options.get('minimum_feature_pixels',4)]
        result['warnings'].append('Threshold components depend on leveling, mask and tip convolution; they are not automatically grains.')
    return result


def analyze_sem(path, options, sidecar=None, source_name=''):
    with Image.open(path) as image:
        width,height = image.size
        image_format = image.format
        if width*height>40_000_000:
            raise ValueError('SEM image exceeds 40 million pixels.')
    metadata,warnings = {},[]
    if sidecar:
        raw = sidecar.read_bytes()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp1252',errors='replace')
            warnings.append('Sidecar decoded using a fallback; verify special unit characters.')
        for line in text.splitlines():
            key,_,value = line.partition(' ')
            if key.startswith('$'):
                metadata[key] = value.strip()
    instrument_date = metadata.get('$CM_DATE')
    folder_match = re.search(r'(\d{2})[.](\d{2})[.](\d{2})',source_name)
    if folder_match and instrument_date:
        folder_date = '20%s-%s-%s' % folder_match.groups()
        try:
            month,day,year = map(int,instrument_date.split('/'))
            if folder_date != f'{year:04d}-{month:02d}-{day:02d}':
                warnings.append(f'Date conflict: folder {folder_date}, instrument {instrument_date}.')
        except ValueError:
            warnings.append('Instrument date could not be parsed.')
    scale = options.get('pixel_size_um')
    suggestion = None
    marker = re.match(r'([\d.]+)\s*(.*)',metadata.get('$$SM_MICRON_MARKER',''))
    bar = metadata.get('$$SM_MICRON_BAR','')
    if marker and bar.isdigit() and int(bar)>0 and marker.group(2) in {'µm','μm','um'}:
        suggestion = float(marker.group(1))/int(bar)
    if options.get('confirm_sidecar_calibration'):
        if suggestion is None or metadata.get('$CM_IMAGE_RES') != f'{width}x{height}':
            raise ValueError('Sidecar calibration cannot be confirmed: units or image resolution do not match.')
        scale = suggestion
    features = []
    for polygon in options.get('polygons',[]):
        if len(polygon)<3 or any(not(0<=p[0]<=width and 0<=p[1]<=height) for p in polygon):
            raise ValueError('A morphology polygon needs at least three vertices inside the image.')
        xy = np.asarray(polygon,dtype=float)
        area = abs(float(np.dot(xy[:,0],np.roll(xy[:,1],1))-np.dot(xy[:,1],np.roll(xy[:,0],1))))/2
        perimeter = float(np.linalg.norm(xy-np.roll(xy,1,axis=0),axis=1).sum())
        features.append({'vertices':polygon,'area_pixels2':area,'perimeter_pixels':perimeter,
            'area_um2':area*scale**2 if scale else None,'equivalent_diameter_um':2*np.sqrt(area/np.pi)*scale if scale else None})
    if not scale:
        warnings.append('Physical dimensions require confirmed calibration. Pixel measurements remain available.')
    return {'summary':{'width':width,'height':height,'feature_count':len(features),'pixel_size_um':scale},
            'format':image_format,'metadata':metadata,'suggested_pixel_size_um':suggestion,'features':features,'warnings':warnings}
